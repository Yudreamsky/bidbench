"""命令行入口：bidbench <命令>。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from . import classify, debuglog, guide, numbering, oplog, ops, overnight, recognition, state, survey
from .store import Project, add_decision, latest_decisions, now_iso, revoke_decision

SOURCE = "命令行"
log = logging.getLogger(__name__)


def _proj(args) -> Project:
    return Project.open(Path(args.project))


def cmd_init(args):
    p = Project.init(Path(args.dir), args.name)
    print(f"已创建项目「{args.name}」：{p.root}")


def cmd_receive(args):
    def progress(n, total):
        if total >= 20:  # 文件多时显示进度
            print(f"\r  已处理 {n}/{total} 个文件", end="" if n < total else "\n", flush=True)

    proj = _proj(args)
    r = ops.receive(proj, [Path(a) for a in args.archive], args.date, args.name, args.password, args.note,
                    av=not args.no_av, progress=progress)
    ops.classify_if_ready(proj)  # 用过 Codex、Claude 识别的：新图纸先按名字分一下类
    return r


def cmd_scan(args):
    return ops.scan(_proj(args), av=not args.no_av)


def cmd_survey(args):
    return survey.make_plan(_proj(args), Path(args.folder), args.executor, args.model)


def cmd_receive_plan(args):
    def progress(n, total):
        if total >= 20:
            print(f"\r  已处理 {n}/{total} 个文件", end="" if n < total else "\n", flush=True)

    only = [int(x) for x in args.only.split(",") if x.strip()] if args.only else None
    return survey.receive_plan(_proj(args), Path(args.plan), args.date, av=not args.no_av, progress=progress, only=only)


def cmd_read_dwg(args):
    proj = _proj(args)
    r = ops.read_cad(proj, again=args.again)
    if not r["files"] and not r["no_tool"]:
        print("没有要读的 DWG、DXF（按旧规则读的会自动重读）。" + ("" if args.again else "要全部重读，加 --again。"))
    return r


def cmd_tasks(args):
    proj = _proj(args)
    t = recognition.make_task(proj, include_done=args.all, render=not args.no_images, scope=args.scope, stages=args.stage)
    if not t:
        print("没有需要识别的页面。")
        return
    n = len(json.loads((t / "manifest_in.json").read_text(encoding="utf-8"))["pages"])
    print(f"已生成识别任务包（{n} 页）：{t}")
    print("交给外部 agent 按 任务说明.md 执行，或运行 bidbench recognize 用内置模拟执行器。")


def cmd_stages(args):
    """分步识别进度：只读，不看图、不花钱。"""
    proj = _proj(args)
    sp = recognition.stage_plan(proj, args.scope)
    sp.pop("_rows"), sp.pop("_hints")
    print(ops.stages_text(sp))
    print("识别某几步：bidbench recognize --executor codex --stage key（可写几次 --stage：key=重点图，main=建模主图，deep=按需深入）")
    return sp


def cmd_recognize(args):
    proj = _proj(args)
    if args.executor == "simulated":
        return ops.recognize_simulated(proj, Path(args.task) if args.task else None)
    if args.effort == "ultra" and args.executor != "codex":
        sys.exit("错误：思考强度 ultra 只有 Codex 能用")
    given = Path(args.task) if args.task else None
    if args.executor == "codex":
        est = ops.prepare_codex(proj, args.scope, args.tier, args.model, args.tiles, given, args.workers, args.stage)
    else:
        est = ops.prepare_claude(proj, args.scope, args.tier, args.model, args.tiles, given, args.stage)
    if est is None:
        print(f"范围「{recognition.SCOPES[args.scope]}」内没有需要识别的页面。")
        return None
    task = Path(est["task"])
    print(f"任务包：{task}")
    print(ops.estimate_text(est))
    if args.dry_run:
        print("只做了估算（--dry-run），没有开始识别。去掉 --dry-run 正式运行，或用 --task 指定这个任务包。")
        return est
    again = f"bidbench recognize --executor {args.executor} --task {task}"
    along = ops.ClassifyAlong(proj, args.executor, args.model)  # 边识别边分类，识别完再分一次
    try:  # 每识别完一页输出一行（识别过程日志）
        if args.executor == "codex":
            r = ops.recognize_codex(proj, task, args.tier, args.model, args.effort, args.tiles, args.workers,
                                    on_import=along)
        else:
            r = ops.recognize_claude(proj, task, args.tier, args.model, args.effort, args.tiles, args.workers or 4,
                                     on_import=along)
    except ops.OpError as e:
        along.wait()
        sys.exit(f"错误：{e}：{again}")
    if r["failed"]:
        print(f"失败的页面可以重跑：{again}")
    along.finish()
    return r


def cmd_classify(args):
    proj = _proj(args)
    try:
        return ops.classify(proj, args.executor, args.model, args.all)
    except ops.OpError as e:
        sys.exit(f"错误：{e}")


def cmd_overnight(args):
    proj = _proj(args)
    until = overnight.deadline(args.until) if args.until else None
    focus = recognition.Focus(_disciplines(args.discipline), args.tower, args.floor)
    try:
        return overnight.run(proj, args.executor, args.scope, args.tier, args.model, args.stage, until, args.wait,
                             args.workers, focus=focus or None)
    except ValueError as e:  # 开始以前查出的：塔楼写错、楼层看不出
        sys.exit(f"错误：{e}")


def _disciplines(text: str | None) -> tuple[str, ...]:
    """“建筑,结构” → ("建筑", "结构")。"""
    out = tuple(x for x in re.split(r"[,，、\s]+", text or "") if x)
    bad = [x for x in out if x not in classify.DISCIPLINES]
    if bad:
        sys.exit(f"错误：不认识的专业 {'、'.join(bad)}，可选：{'、'.join(classify.DISCIPLINES)}")
    return out


def cmd_import(args):
    proj = _proj(args)
    res = recognition.import_results(proj, Path(args.results))
    print(f"导入 {res.imported} 页，{res.unchanged} 页无变化。")
    if res.errors:
        print(f"{len(res.errors)} 个文件未通过校验，详见 reports/识别结果导入问题.md")
        for e in res.errors[:5]:
            print(f"  {e['file']}：{e['error']}")


def cmd_build(args):
    return ops.build(_proj(args), args.as_of)


def cmd_decide(args):
    proj = _proj(args)
    if args.kind == "numbering":  # 值是 off（先不用）或一份图号格式的 JSON
        try:
            args.value = numbering.decision_value(args.value)
        except numbering.FormatError as e:
            print(f"图号格式不对：{e}")
            return 1
    add_decision(proj, args.kind, args.key, args.value, args.note)
    print(f"已记录决定：{args.kind} {args.key} = {args.value}")


def cmd_revoke(args):
    proj = _proj(args)
    with proj.connect() as con:
        d = latest_decisions(con).get((args.kind, args.key))
    if not d:
        sys.exit(f"错误：{args.kind} {args.key} 没有做过决定，或者已经撤回了")
    revoke_decision(proj, args.kind, args.key, args.note)
    print(f"已撤回：{guide.decision_text(args.kind, args.key, d['value'])}。问题会重新出现，可以重新决定。")


def cmd_baseline(args):
    proj = _proj(args)
    snap = state.create_baseline(proj, args.name, args.sheets.split(",") if args.sheets else None, args.note)
    print(f"已锁定基线「{args.name}」：{len(snap)} 张图。")


def cmd_export(args):
    return {"path": str(ops.export_(_proj(args), args.what, Path(args.out) if args.out else None))}


def cmd_mcp(args):
    try:
        from .mcp_server import run_stdio
    except ImportError:
        sys.exit("错误：需要先安装 MCP SDK：pip install -e \".[mcp]\"")
    from .assistant import Toolkit
    run_stdio(Toolkit(_proj(args)))


def cmd_connect_codex(args):
    from . import codexlink
    proj = _proj(args)
    if args.print:
        print(codexlink.server_block(proj), end="")
        return
    r = codexlink.connect(proj)
    print(f"已把项目接到 Codex：{r['path']}" + ("（替换了原来的 bidbench 设置）" if r["replaced"] else ""))
    if r["backup"]:
        print(f"原来的设置备份在：{r['backup']}")
    print("重新打开 Codex（命令行、桌面版或编辑器插件）后就能用 bidbench 的工具；输入 /mcp 可以查看。")


def cmd_chatgpt(args):
    from . import chatgpt
    if args.action == "logout":
        r = chatgpt.logout()
        print({True: "已退出 ChatGPT 账号，OpenAI 那边的授权也已吊销。",
               False: "已退出，本机的登录信息已删除；但没能确认 OpenAI 那边已吊销，可以到 ChatGPT 设置里断开 bidbench。",
               None: "本来就没有登录。"}[r["revoked"]])
        return
    if args.action == "login":
        lg = chatgpt.start_login(new_account=args.new_account)
        print("已在浏览器里打开 ChatGPT 的授权页。没有自动打开的话，复制下面的网址到浏览器：")
        print(lg.url)
        print("等你在浏览器里登录、同意……（最多等十分钟，Ctrl+C 取消）")
        lg.wait()
        lg.close()
        if lg.error:
            sys.exit(f"错误：{lg.error}")
    st = chatgpt.status()
    if not st["signed_in"]:
        print("还没有用 ChatGPT 账号登录。登录：bidbench chatgpt login")
        return
    print(f"已用 ChatGPT 账号登录：{st['email'] or '（没有邮箱）'}")
    print(st["hint"] or "识别、智能分类会直接调用 OpenAI 模型，用 ChatGPT 套餐的额度，不再经过 Codex 命令行。")
    print(f"用量：{st['usage_url']}")


def cmd_proposals(args):
    from .assistant import Toolkit
    rows = Toolkit(_proj(args), source="命令行").list_proposals("all" if args.all else "pending")
    if not rows:
        print("没有待确认的 AI 助手提议。" if not args.all else "还没有任何提议。")
        return
    label = {"pending": "待确认", "confirmed": "已确认", "rejected": "已驳回", "stale": "已失效"}
    for r in rows:
        print(f"#{r['id']} [{label.get(r['status'], r['status'])}] {r['summary']}")
        print(f"    理由：{r['reason']}")
        if r["outcome"]:
            print(f"    结果：{r['outcome']}")


def cmd_confirm(args):
    from .assistant import Toolkit, ToolError
    kit = Toolkit(_proj(args), source="命令行")
    try:
        r = kit.confirm(args.id)
    except ToolError as e:
        sys.exit(f"错误：{e}")
    print(f"提议 #{r['proposal_id']}：{r['outcome']}")


def cmd_reject(args):
    from .assistant import Toolkit, ToolError
    try:
        r = Toolkit(_proj(args), source="命令行").reject(args.id, args.note)
    except ToolError as e:
        sys.exit(f"错误：{e}")
    print(f"已驳回提议 #{r['proposal_id']}。")


def cmd_notes(args):
    from .assistant import Toolkit
    rows = Toolkit(_proj(args)).list_notes()
    if not rows:
        print("还没有项目笔记。")
    for n in rows:
        print(f"[{n['topic']}] {n['text']}（{n['source']}，{n['created_at'][:10]}）")


def cmd_log(args):
    proj = _proj(args)
    rows = oplog.read(proj, args.limit)[::-1]  # 最新的在最下面，紧挨着命令提示符
    if not rows:
        print("还没有操作记录。")
    day = None
    for e in rows:
        t = oplog.when(e["time"])
        if t.date() != day:
            day = t.date()
            print(f"\n{day:%Y-%m-%d}")
        who = e.get("source") or e.get("actor") or ""
        state_word = {"error": " —— 出错", "interrupted": " —— 中断"}.get(e.get("status"), "")
        print(f"  {t:%H:%M}  {e['title']}" + (f" · {who}" if who else "") + state_word)
        for extra in (e.get("summary"), e.get("error") and f"错误：{e['error']}"):
            if extra:
                print(f"         {extra}")
        if args.lines:
            for ln in e.get("lines") or []:
                print(f"           {ln}")
    print(f"\n项目操作日志：{proj.root / oplog.FOLDER}")
    print(f"排错日志（给开发用）：{debuglog.folder()}")


def cmd_suggestions(args):
    from .assistant import list_suggestions, suggestions_path
    rows = list_suggestions()
    if not rows:
        print("还没有确认过的改进建议。")
    for n, r in enumerate(rows, 1):
        print(f"{n}. 【{r['title']}】（{str(r.get('time', ''))[:10]}）")
        print(f"   问题：{r.get('problem', '')}")
        print(f"   建议：{r.get('suggestion', '')}")
    print(f"\n清单文件：{suggestions_path()}")


def cmd_desktop(args):
    from .desktop import main as desktop_main
    project = args.project if (Path(args.project) / "bidbench.json").exists() else None
    sys.exit(desktop_main(([project] if project else []) + (["--browser"] if args.browser else [])))


def _update_installed(args):
    """安装版：从发布仓库下载新安装包，静默安装。"""
    from . import release, updater
    if args.rollback:
        try:
            r = release.rollback(progress=_download_progress)
        except updater.UpdateError as e:
            sys.exit(f"\n错误：{e}")
        print(f"\n已下载 {r['rolled_back_to']}，正在安装。安装完成后工作台会自动打开。")
        release.launch_installer(r["installer"])
        return
    print("正在检查更新……")
    st = release.check()
    print(f"当前版本 {st['current']['version']}（构建 b{st['current']['build']}）")
    print(st["message"])
    if st["status"] != "available":
        if st["status"] == "error":
            sys.exit(1)
        return
    for c in st["commits"]:
        print(f"  {c['date']}  {c['title']}")
    if args.check:
        return
    if not args.yes and not _confirm("确认更新？[y/N] "):
        print("已取消。")
        return
    try:
        r = release.apply(progress=_download_progress)
    except updater.UpdateError as e:
        sys.exit(f"\n错误：{e}")
    print(f"\n已下载 {r['updated_to']}，正在安装。请关闭正在运行的工作台；安装完成后会自动打开。")
    release.launch_installer(r["installer"])


def _download_progress(done, total):
    print(f"\r  已下载 {done / 1048576:.1f}" + (f" / {total / 1048576:.1f}" if total else "") + " MB", end="", flush=True)


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def cmd_update(args):
    from . import release, updater
    if release.is_installed():
        return _update_installed(args)
    if args.rollback:
        try:
            r = updater.rollback(run_pip=None)
        except updater.UpdateError as e:
            sys.exit(f"错误：{e}")
        print(f"已回退：{r['from']} → {r['rolled_back_to']}")
        _finish_pip(updater, r["pip_command"])
        return
    print("正在检查更新……")
    st = updater.check()
    cur = st["current"]
    print(f"当前版本 {cur['version']}（{cur['commit'] or '未知'}）")
    if st["status"] != "available":
        print(st["message"])
        if st["status"] in ("error", "not_git"):
            sys.exit(1)
        return
    print(st["message"])
    for c in st["commits"]:
        print(f"  {c['date']}  {c['title']}")
    if st["deps_changed"]:
        print("这次更新改动了依赖，更新后会自动补装。")
    if args.check:
        return
    if not args.yes and not _confirm("确认更新？[y/N] "):
        print("已取消。")
        return
    try:
        r = updater.apply(run_pip=None)
    except updater.UpdateError as e:
        sys.exit(f"错误：{e}")
    print(f"代码已更新到 {r['updated_to']}（之前是 {r['previous']}，可用 bidbench update --rollback 回退）。")
    _finish_pip(updater, r["pip_command"])
    print("正在运行的工作台需要重启才能用上新版本。")


def _finish_pip(updater, cmd):
    """补装依赖。Windows 上 bidbench.exe 正在运行时无法覆盖，改为本窗口退出后在后台安装。"""
    if not cmd:
        return
    running_exe = os.name == "nt" and sys.argv and sys.argv[0].lower().endswith((".exe", "bidbench"))
    if running_exe:
        root = updater.repo_root()
        log = root / ".git" / "bidbench_update.log"
        updater.spawn_helper(os.getpid(), cmd, None, log)
        print(f"依赖会在本命令结束后自动在后台安装，过程记录在 {log}。")
        return
    print("正在补装依赖……")
    import subprocess
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("错误：依赖安装失败，请手动运行：" + " ".join(cmd))


def cmd_serve(args):
    from .server import serve
    proj = None
    if (Path(args.project) / "bidbench.json").exists():
        proj = _proj(args)
        proj.connect().close()  # 先打开一次数据库：需要升级时现在就升级，版本过新时现在就报错
    httpd = serve(proj, args.host, args.port)
    print(f"工作台已启动：http://{args.host}:{args.port}/  （Ctrl+C 退出）"
          + ("" if proj else "\n当前目录不是项目，打开后可以在页面里选择或新建项目。"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def _version_text() -> str:
    from . import __version__
    from ._build import BUILD, COMMIT
    return f"bidbench {__version__}" + (f"（构建 b{BUILD}，{COMMIT[:7]}）" if BUILD else "")


# ---------------- 日志 ----------------

def _op(args) -> tuple[str, str, str] | None:
    """会改动项目的命令：(类型, 标题, 项目目录)，记进项目的操作日志。查询类命令和长时间运行的服务不记。"""
    c = args.cmd
    tier = "经济档" if getattr(args, "tier", "") == "economy" else "强档"
    if c == "init":
        return "init", f"新建项目「{args.name}」", args.dir
    if c == "receive":
        return "receive", oplog.receive_title(args.archive), args.project
    if c == "survey":
        return "survey", f"{oplog.TITLES['survey']}：{Path(args.folder).name or args.folder}", args.project
    if c == "receive-plan":
        return "receive_plan", f"{oplog.TITLES['receive_plan']}：{Path(args.plan).name}", args.project
    if c == "recognize":
        if args.executor == "simulated":
            return "recognize", oplog.TITLES["simulated"], args.project
        who = "Codex" if args.executor == "codex" else "Claude"
        if args.dry_run:
            return "estimate", f"{oplog.TITLES['estimate']}（{who} {tier}）", args.project
        return "recognize", f"{oplog.TITLES[args.executor]}（{tier}）", args.project
    if c == "classify":
        return "classify", f"{oplog.TITLES['classify']}（{ops.CLASSIFY_BY[args.executor]}{'，全部重新分' if args.all else ''}）", args.project
    if c == "build":
        return "build", oplog.TITLES["build"] + (f"（截至批次 #{args.as_of}）" if args.as_of else ""), args.project
    if c == "export":
        return "export", oplog.TITLES[args.what], args.project
    if c == "read-dwg":
        return "read_dwg", oplog.TITLES["read_dwg"] + ("（全部重读）" if args.again else ""), args.project
    if c == "overnight":
        return "overnight", f"{oplog.TITLES['overnight']}（{ops.CLASSIFY_BY[args.executor]} {tier}）", args.project
    if c in ("scan", "tasks", "import"):
        return c, oplog.TITLES[c], args.project
    return None


class _Tee(io.TextIOBase):
    """命令的输出照常显示，同时留一份写进操作日志。进度行（\r 覆盖）只留最后的样子。"""

    def __init__(self, out):
        self.out, self.buf, self.lines = out, "", []

    @property
    def encoding(self):
        return getattr(self.out, "encoding", "utf-8")

    def isatty(self):
        return self.out.isatty()

    def write(self, s):
        self.out.write(s)
        *done, self.buf = (self.buf + s).split("\n")
        self.lines += [ln.rsplit("\r", 1)[-1] for ln in done]
        return len(s)

    def flush(self):
        self.out.flush()

    def finish(self) -> list[str]:
        if self.buf.strip():
            self.lines.append(self.buf.rsplit("\r", 1)[-1])
        return self.lines


def _run(args):
    op = _op(args)
    if op is None:
        return args.fn(args)
    kind, title, root = op
    tee, started, t0 = _Tee(sys.stdout), now_iso(), time.monotonic()
    status, error, result = "done", None, None
    try:
        with contextlib.redirect_stdout(tee):
            result = args.fn(args)
        return result
    except SystemExit as e:
        if e.code not in (None, 0):
            status, error = "error", str(e.code).removeprefix("错误：")
        raise
    except KeyboardInterrupt:
        status, error = "interrupted", "按 Ctrl+C 中断"
        raise
    except Exception as e:
        status, error = "error", str(e) if isinstance(e, (ValueError, OSError)) else f"{type(e).__name__}: {e}"
        raise
    finally:
        if (Path(root) / "bidbench.json").exists():  # 新建项目失败等情况下没有项目，不记
            oplog.write(Project.open(Path(root)), kind, title, source=SOURCE, status=status, lines=tee.finish(),
                        error=error, started=started, seconds=time.monotonic() - t0, result=result)


def _utf8_output() -> None:
    """输出一律用 UTF-8。Windows 上输出被重定向或接管道时默认是本地代码页（cp936 / cp1252），中文和符号会报错。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv=None):
    _utf8_output()
    ap = argparse.ArgumentParser(prog="bidbench", description="投标图纸工作台")
    ap.add_argument("-p", "--project", default=".", help="项目目录（默认当前目录）")
    ap.add_argument("--version", action="version", version=_version_text())
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="新建项目")
    s.add_argument("dir")
    s.add_argument("--name", required=True)
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("receive", help="收图：把一个压缩包、文件、文件夹（或同时收到的几个文件）登记为新批次并递归解包")
    s.add_argument("archive", nargs="+", help="压缩包、文件或文件夹；给多个文件时算同一批")
    s.add_argument("--date", required=True, help="收图日期 YYYY-MM-DD")
    s.add_argument("--name")
    s.add_argument("--password", action="append", default=[], help="压缩包密码，可多次指定")
    s.add_argument("--note")
    s.add_argument("--no-av", action="store_true", help="不调用杀毒软件（静态检查照做）")
    s.set_defaults(fn=cmd_receive)

    s = sub.add_parser("survey", help="智能收图：给一级目录（比如整个共享盘），只列文件夹不读文件内容，判断收哪些子文件夹、"
                                      "分几批，写成收图计划（项目 reports 里）")
    s.add_argument("folder")
    s.add_argument("--executor", default="codex", choices=["codex", "rules"],
                   help="codex=让 Codex 判断（只发文件夹名字、各类文件数量和几个图纸文件名）；rules=只按名字和数量的规则")
    s.add_argument("--model", help="换 Codex 模型")
    s.set_defaults(fn=cmd_survey)

    s = sub.add_parser("receive-plan", help="按收图计划一批批收（bidbench survey 写的 json）；收过的批次跳过，断了再运行接着收")
    s.add_argument("plan")
    s.add_argument("--date", required=True, help="收图日期 YYYY-MM-DD")
    s.add_argument("--only", help="只收这几批，如 1,3,4")
    s.add_argument("--no-av", action="store_true", help="不调用杀毒软件（静态检查照做）")
    s.set_defaults(fn=cmd_receive_plan)

    s = sub.add_parser("scan", help="风险文件检查：按最新规则重查全部文件，并用本机杀毒软件扫描")
    s.add_argument("--no-av", action="store_true", help="只做静态检查")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("read-dwg", help="读 DWG、DXF 的图签和文字（收图后会自动读；装好 LibreDWG 后补读以前收的）")
    s.add_argument("--again", action="store_true", help="全部重读（认图签的规则更新以后用）")
    s.set_defaults(fn=cmd_read_dwg)

    s = sub.add_parser("tasks", help="生成识别任务包，交给外部 agent 执行")
    s.add_argument("--scope", default="new", choices=list(recognition.SCOPES), help="挑选哪些页面（默认 new）")
    s.add_argument("--all", action="store_true", help="等同于 --scope all")
    s.add_argument("--no-images", action="store_true", help="不渲染页面图片（仅文字层）")
    s.add_argument("--stage", action="append", choices=[recognition.MAP_STAGE, *(k for k, _l, _d in recognition.STAGES)],
                   help="只识别分步识别的这几步（可写几次）：" + "；".join(f"{k}={lb}" for k, lb, _d in recognition.STAGES)
                        + f"；{recognition.MAP_STAGE}=只识别图纸目录、发图单（建大地图）")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("stages", help="分步识别的进度：地图认出了多少，重点图、建模主图、按需深入各还剩几页")
    s.add_argument("--scope", default="new", choices=list(recognition.SCOPES), help="“还要识别”按哪个范围算（默认 new）")
    s.set_defaults(fn=cmd_stages)

    s = sub.add_parser("recognize", help="用内置执行器识别并导入")
    s.add_argument("--executor", default="simulated", choices=["simulated", "claude", "codex"],
                   help="simulated=模拟（演示）；claude=Anthropic API，按量付费；codex=本机的 Codex，用 ChatGPT 账号的额度")
    s.add_argument("--task", help="已有任务包路径（续跑）；不填则新建")
    s.add_argument("--scope", default="new", choices=list(recognition.SCOPES),
                   help="claude 执行器挑选哪些页面：" + "；".join(f"{k}={v}" for k, v in recognition.SCOPES.items()))
    s.add_argument("--tier", default="economy", choices=["economy", "strong"],
                   help="economy=首次全量识别（Claude：claude-haiku-5-5；Codex：gpt-6.1-sol 中等思考）；"
                        "strong=加局部切片，用于复核（Claude：claude-opus-5-5；Codex：gpt-6.1-sol 深入思考）")
    s.add_argument("--model", help="覆盖档位的默认模型（Codex 能用的模型见 codex debug models）")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"],
                   help="覆盖档位的默认思考强度（ultra 只有 Codex 能用）")
    s.add_argument("--tiles", type=int, help="局部切片 N×N，0 表示不切")
    s.add_argument("--workers", type=int, help="同时识别几页（Claude 默认 4，Codex 默认 3）")
    s.add_argument("--dry-run", action="store_true", help="只生成任务包并估算花费，不调用 API")
    s.add_argument("--stage", action="append", choices=[recognition.MAP_STAGE, *(k for k, _l, _d in recognition.STAGES)],
                   help="只识别分步识别的这几步（可写几次）：" + "；".join(f"{k}={lb}" for k, lb, _d in recognition.STAGES)
                        + f"；{recognition.MAP_STAGE}=只识别图纸目录、发图单（建大地图）")
    s.set_defaults(fn=cmd_recognize)

    s = sub.add_parser("classify", help="智能分类：让 AI 给图纸分专业、分小类、起中文简称，给事实分多级目录")
    s.add_argument("--executor", default="codex", choices=["codex", "claude", "simulated"],
                   help="codex=本机的 Codex（ChatGPT 账号的额度）；claude=Anthropic API，按量付费；simulated=按规则分（演示）")
    s.add_argument("--model", help="换模型（Claude 默认 claude-sonnet-5-5，Codex 默认 gpt-6.1-sol）")
    s.add_argument("--all", action="store_true", help="全部重新分；默认只分还没分过的")
    s.set_defaults(fn=cmd_classify)

    s = sub.add_parser("overnight", help="整夜识别：读 DWG、分步识别、智能分类、算报告一口气做完；单个文件出错跳过，"
                                         "额度用完等着续跑，过程写进项目 logs 文件夹；断了再运行一次接着做")
    s.add_argument("--executor", default="codex", choices=list(overnight.EXECUTORS),
                   help="codex=本机的 Codex（ChatGPT 账号的额度）；claude=Anthropic API，按量付费")
    s.add_argument("--scope", default="new", choices=list(recognition.SCOPES), help="识别哪些页面（默认 new）")
    s.add_argument("--tier", default="economy", choices=["economy", "strong"], help="同 recognize")
    s.add_argument("--model", help="覆盖档位的默认模型")
    s.add_argument("--stage", action="append", choices=[recognition.MAP_STAGE, *(k for k, _l, _d in recognition.STAGES)],
                   help="只识别这几步（可写几次）；默认重点图、建模主图、按需深入依次都做，聚焦时先识别图纸目录（map）建大地图")
    s.add_argument("--until", help="最晚做到几点（时:分，如 07:30）：到点不再开始新的页、不再等额度，照样分类、出报告；不写就做完为止")
    s.add_argument("--wait", type=float, default=overnight.WAIT_MINUTES,
                   help=f"额度用完后隔几分钟再试（默认 {overnight.WAIT_MINUTES}）")
    s.add_argument("--workers", type=int, help="同时识别几页（Claude 默认 4，Codex 默认 3）")
    s.add_argument("--discipline", help="聚焦：只识别这几个专业的图，如 建筑,结构（专业按图纸台账认；图号认不出专业的，先智能分类按图名、文件夹认）")
    s.add_argument("--tower", help="聚焦：只识别这栋楼的图，如 T1（几栋共用的裙楼、地下室和看不出塔楼的也算）")
    s.add_argument("--floor", help="聚焦：只识别建这一层要用的图，如 \"LEVEL 09\"（按项目英文楼层的数法换算）或 9F；"
                                   "这一层的图、标准层、通用图、剖面立面轴网目录和表，再跟着索引识别详图")
    s.set_defaults(fn=cmd_overnight)

    s = sub.add_parser("import", help="校验并导入识别结果")
    s.add_argument("results")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("build", help="计算台账、缺图、事实层并写报告")
    s.add_argument("--as-of", type=int, help="还原到第几批的状态")
    s.set_defaults(fn=cmd_build)

    kinds = ["sheet_pick", "sheet_current", "sheet_void", "missing", "fact", "risk", "catalog_rev",
             "name_check", "file_tower", "tower_alias", "rev_check", "floor_base", "numbering"]
    s = sub.add_parser("decide", help="记录人工决定")
    s.add_argument("kind", choices=kinds)
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("revoke", help="撤回一个人工决定（问题会重新出现）")
    s.add_argument("kind", choices=kinds)
    s.add_argument("key")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_revoke)

    s = sub.add_parser("baseline", help="为建模等任务锁定图纸基线")
    s.add_argument("name")
    s.add_argument("--sheets", help="逗号分隔的图号；不填为全部现行图纸")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("export", help="导出现行图纸文件夹或 agent 数据镜像")
    s.add_argument("what", choices=["current", "mirror"])
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("mcp", help="以 MCP 服务方式运行（stdio），供 Claude 桌面版、Claude Code 等 agent 调用")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("connect-codex", help="把这个项目接到本机的 Codex（写进 ~/.codex/config.toml）")
    s.add_argument("--print", action="store_true", help="只显示要写的设置，不写")
    s.set_defaults(fn=cmd_connect_codex)

    s = sub.add_parser("chatgpt", help="用 ChatGPT 账号登录：识别、分类直接调用 OpenAI 模型，用套餐额度，不用装 Codex")
    s.add_argument("action", nargs="?", default="status", choices=["login", "logout", "status"])
    s.add_argument("--new-account", action="store_true", help="换个账号登录（重新登记）")
    s.set_defaults(fn=cmd_chatgpt)

    s = sub.add_parser("proposals", help="查看 AI 助手的提议")
    s.add_argument("--all", action="store_true", help="包括已处理的提议")
    s.set_defaults(fn=cmd_proposals)

    s = sub.add_parser("confirm", help="确认一条 AI 助手提议，使其生效")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_confirm)

    s = sub.add_parser("reject", help="驳回一条 AI 助手提议")
    s.add_argument("id", type=int)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser("notes", help="查看已确认的项目笔记")
    s.set_defaults(fn=cmd_notes)

    s = sub.add_parser("update", help="从 GitHub 更新到最新版本")
    s.add_argument("--check", action="store_true", help="只检查，不更新")
    s.add_argument("--yes", action="store_true", help="不询问，直接更新")
    s.add_argument("--rollback", action="store_true", help="回到上一次更新之前的版本")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("suggestions", help="查看 AI 助手提出、经你确认的改进建议（给开发用）")
    s.set_defaults(fn=cmd_suggestions)

    s = sub.add_parser("log", help="查看操作日志：收图、识别、导出、人工决定、AI 助手提议的时间线")
    s.add_argument("-n", "--limit", type=int, default=50, help="显示最近多少条（默认 50）")
    s.add_argument("--lines", action="store_true", help="同时显示每次操作的运行记录")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("desktop", help="打开桌面工作台（独立窗口）")
    s.add_argument("--browser", action="store_true", help="用默认浏览器打开")
    s.set_defaults(fn=cmd_desktop)

    s = sub.add_parser("serve", help="启动本地工作台（浏览器）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    debuglog.setup(args.cmd)
    log.info("命令：%s", debuglog.safe_command(["bidbench", *(sys.argv[1:] if argv is None else argv)]))
    t0 = time.monotonic()
    try:
        _run(args)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        log.info("命令没有完成：%s", e)
        sys.exit(f"错误：{e}")
    except SystemExit as e:
        if e.code not in (None, 0):
            log.info("命令没有完成：%s", e.code)
        raise
    except KeyboardInterrupt:
        log.info("命令被中断")
        raise
    except Exception:
        log.exception("命令意外出错")
        raise
    log.info("命令结束，用时 %.1f 秒", time.monotonic() - t0)


if __name__ == "__main__":
    main()
