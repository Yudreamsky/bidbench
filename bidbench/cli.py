"""命令行入口：bidbench <命令>。"""

from __future__ import annotations

import argparse
import os
import json
import sys
from pathlib import Path

from . import export, ingest, recognition, reports, state
from .executors import simulated
from .store import Project, add_decision


def _proj(args) -> Project:
    return Project.open(Path(args.project))


def cmd_init(args):
    p = Project.init(Path(args.dir), args.name)
    print(f"已创建项目「{args.name}」：{p.root}")


def cmd_receive(args):
    proj = _proj(args)
    r = ingest.receive(proj, Path(args.archive), args.date, args.name, args.password, args.note)
    rep = reports.receive_report(proj, r)
    kinds = "、".join(f"{reports.KIND_LABEL.get(k, k)} {n}" for k, n in sorted(r.by_kind.items()))
    print(f"批次 #{r.seq}「{r.name}」：解出 {r.files} 个文件（{kinds}），新增 {r.new_pages} 页。")
    if r.nested_archives:
        print(f"  内层压缩包 {len(r.nested_archives)} 个，已自动展开。")
    for i in r.issues:
        print(f"  ⚠ {reports.ISSUE_LABEL.get(i['kind'], i['kind'])}：{i['path']}")
    print(f"解包报告：{rep}")


def cmd_tasks(args):
    proj = _proj(args)
    t = recognition.make_task(proj, include_done=args.all, render=not args.no_images, scope=args.scope)
    if not t:
        print("没有需要识别的页面。")
        return
    n = len(json.loads((t / "manifest_in.json").read_text(encoding="utf-8"))["pages"])
    print(f"已生成识别任务包（{n} 页）：{t}")
    print("交给外部 agent 按 任务说明.md 执行，或运行 bidbench recognize 用内置模拟执行器。")


def cmd_recognize(args):
    proj = _proj(args)
    if args.executor == "simulated":
        task = Path(args.task) if args.task else recognition.make_task(proj, render=False, scope="unrecognized")
        if task is None:
            print("没有需要识别的页面。")
            return
        n = simulated.run(task)
        res = recognition.import_results(proj, task / "results")
        print(f"模拟执行器识别 {n} 页，导入 {res.imported} 页。" + (f"{len(res.errors)} 个文件未通过校验。" if res.errors else ""))
        return

    from .executors import claude
    task = Path(args.task) if args.task else recognition.make_task(proj, render=True, scope=args.scope)
    if task is None:
        print(f"范围「{recognition.SCOPES[args.scope]}」内没有需要识别的页面。")
        return
    est = claude.estimate(task, args.tier, args.model, args.tiles)
    usd = f"约 ${est['estimated_usd']:.2f}" if est["estimated_usd"] is not None else "未知（模型不在价格表里）"
    print(f"任务包：{task}")
    print(f"待识别 {est['pages']} 页，{est['images']} 张图片，模型 {est['model']}，粗略估算输入 {est['input_tokens']} tokens、"
          f"输出 {est['output_tokens']} tokens，花费{usd}。")
    if args.dry_run:
        print("只做了估算（--dry-run），没有调用 API。去掉 --dry-run 正式运行，或用 --task 指定这个任务包。")
        return

    def progress(n, total):
        print(f"\r  已完成 {n}/{total}", end="", flush=True)

    try:
        r = claude.run(task, args.tier, args.model, args.effort, args.tiles, args.workers, progress=progress)
    except claude.FatalError as e:
        print()
        sys.exit(f"错误：{e}。任务包已保留，配置好后可续跑：bidbench recognize --executor claude --task {task}")
    print()
    cost = r.usage.cost(r.model)
    print(f"识别 {r.done} 页，跳过 {r.skipped} 页，失败 {len(r.failed)} 页；实际用量 输入 {r.usage.input} / 输出 {r.usage.output} tokens"
          + (f"，约 ${cost:.4f}" if cost is not None else "") + "。")
    for f in r.failed[:5]:
        print(f"  ✗ {f['file']}：{f['error']}")
    res = recognition.import_results(proj, task / "results")
    print(f"导入 {res.imported} 页。" + (f"{len(res.errors)} 个文件未通过校验，详见 reports/识别结果导入问题.md" if res.errors else ""))
    if r.failed:
        print(f"失败的页面可以重跑：bidbench recognize --executor claude --task {task}")


def cmd_import(args):
    proj = _proj(args)
    res = recognition.import_results(proj, Path(args.results))
    print(f"导入 {res.imported} 页，{res.unchanged} 页无变化。")
    if res.errors:
        print(f"{len(res.errors)} 个文件未通过校验，详见 reports/识别结果导入问题.md")
        for e in res.errors[:5]:
            print(f"  {e['file']}：{e['error']}")


def cmd_build(args):
    proj = _proj(args)
    st = state.build(proj, args.as_of)
    is_latest = st["as_of"] == max((b["seq"] for b in st["batches"]), default=0)
    sub = None if is_latest else f"截至批次{st['as_of']:02d}"
    out = proj.path("exports", "state.json") if is_latest else proj.path("reports", sub, "state.json")
    out.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    files = reports.write_all(proj, st, sub)
    crit = sum(1 for i in st["issues"] if i["level"] == "crit")
    warn = sum(1 for i in st["issues"] if i["level"] == "warn")
    print(f"截至批次 #{st['as_of']}：{len(st['sheets'])} 个图号，严重问题 {crit} 项，提醒 {warn} 项。")
    for i in st["issues"]:
        if i["level"] != "info":
            print(f"  {'●' if i['level'] == 'crit' else '○'} [{i['kind']}] {i['title']}")
    print(("历史状态报告（reports/" + sub + "）：" if sub else "报告：") + "、".join(p.name for p in files))


def cmd_decide(args):
    proj = _proj(args)
    add_decision(proj, args.kind, args.key, args.value, args.note)
    print(f"已记录决定：{args.kind} {args.key} = {args.value}")


def cmd_baseline(args):
    proj = _proj(args)
    snap = state.create_baseline(proj, args.name, args.sheets.split(",") if args.sheets else None, args.note)
    print(f"已锁定基线「{args.name}」：{len(snap)} 张图。")


def cmd_export(args):
    proj = _proj(args)
    st = state.build(proj, with_diff=False)
    if args.what == "current":
        print(f"现行图纸已导出：{export.export_current(proj, st, Path(args.out) if args.out else None)}")
    else:
        print(f"数据镜像已导出：{export.export_mirror(proj, st, Path(args.out) if args.out else None)}")


def cmd_mcp(args):
    try:
        from .mcp_server import run_stdio
    except ImportError:
        sys.exit("错误：需要先安装 MCP SDK：pip install -e \".[mcp]\"")
    from .assistant import Toolkit
    run_stdio(Toolkit(_proj(args)))


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


def cmd_update(args):
    from . import updater
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
    if not args.yes:
        try:
            ans = input("确认更新？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes", "是"):
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
    proj = _proj(args)
    proj.connect().close()  # 先打开一次数据库：需要升级时现在就升级，版本过新时现在就报错
    httpd = serve(proj, args.host, args.port)
    print(f"工作台已启动：http://{args.host}:{args.port}/  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bidbench", description="投标图纸工作台")
    ap.add_argument("-p", "--project", default=".", help="项目目录（默认当前目录）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="新建项目")
    s.add_argument("dir")
    s.add_argument("--name", required=True)
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("receive", help="收图：登记一个压缩包为新批次并递归解包")
    s.add_argument("archive")
    s.add_argument("--date", required=True, help="收图日期 YYYY-MM-DD")
    s.add_argument("--name")
    s.add_argument("--password", action="append", default=[], help="压缩包密码，可多次指定")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_receive)

    s = sub.add_parser("tasks", help="生成识别任务包，交给外部 agent 执行")
    s.add_argument("--scope", default="new", choices=list(recognition.SCOPES), help="挑选哪些页面（默认 new）")
    s.add_argument("--all", action="store_true", help="等同于 --scope all")
    s.add_argument("--no-images", action="store_true", help="不渲染页面图片（仅文字层）")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("recognize", help="用内置执行器识别并导入")
    s.add_argument("--executor", default="simulated", choices=["simulated", "claude"])
    s.add_argument("--task", help="已有任务包路径（续跑）；不填则新建")
    s.add_argument("--scope", default="new", choices=list(recognition.SCOPES),
                   help="claude 执行器挑选哪些页面：" + "；".join(f"{k}={v}" for k, v in recognition.SCOPES.items()))
    s.add_argument("--tier", default="economy", choices=["economy", "strong"],
                   help="economy=claude-haiku-5-5 全量识别；strong=claude-opus-5-5 加局部切片，用于复核")
    s.add_argument("--model", help="覆盖档位的默认模型")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="覆盖档位的默认 effort")
    s.add_argument("--tiles", type=int, help="局部切片 N×N，0 表示不切")
    s.add_argument("--workers", type=int, default=4, help="并发页数")
    s.add_argument("--dry-run", action="store_true", help="只生成任务包并估算花费，不调用 API")
    s.set_defaults(fn=cmd_recognize)

    s = sub.add_parser("import", help="校验并导入识别结果")
    s.add_argument("results")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("build", help="计算台账、缺图、事实层并写报告")
    s.add_argument("--as-of", type=int, help="还原到第几批的状态")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("decide", help="记录人工决定")
    s.add_argument("kind", choices=["sheet_pick", "sheet_current", "sheet_void", "missing", "fact"])
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_decide)

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

    s = sub.add_parser("serve", help="启动本地工作台")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        sys.exit(f"错误：{e}")


if __name__ == "__main__":
    main()
