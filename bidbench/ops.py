"""日常操作：收图、识别、计算报告、导出、安全扫描。命令行和桌面工作台共用，输出通过 log 回调给人看。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Callable

from . import antivirus, export, ingest, recognition, reports, risk, state
from .store import Project

Log = Callable[[str], None]


class OpError(Exception):
    """操作没法进行。消息直接给用户看。"""


# ---------------- 收图 ----------------

def receive(proj: Project, sources, date: str, name: str | None = None, passwords=(), note: str | None = None,
            av: bool = True, log: Log = print, progress=None) -> dict:
    """收一批图纸：sources 是一个压缩包、文件或文件夹，或者同时收到的几个文件（列表）。"""
    srcs = [Path(s) for s in sources] if isinstance(sources, (list, tuple)) else [Path(sources)]
    r = ingest.receive(proj, srcs if len(srcs) > 1 else srcs[0], date, name, list(passwords), note, progress=progress)
    kinds = "、".join(f"{reports.KIND_LABEL.get(k, k)} {n}" for k, n in sorted(r.by_kind.items()))
    how = {"folder": "（文件夹）", "files": f"（{len(srcs)} 个文件）"}.get(r.source, "")
    verb = "收进" if r.source in ("folder", "files") else "解出"
    log(f"批次 #{r.seq}「{r.name}」{how}：{verb} {r.files} 个文件（{kinds}），新增 {r.new_pages} 页。")
    if r.skipped:
        log(f"  跳过 {len(r.skipped)} 个系统自动生成的文件（Thumbs.db、Office 临时锁文件等）。")
    if r.nested_archives:
        log(f"  内层压缩包 {len(r.nested_archives)} 个，已自动展开。")
    for i in r.issues:
        if i["kind"] != "blocked":  # 被拦下的文件在下面的风险文件里列出
            log(f"  ⚠ {reports.ISSUE_LABEL.get(i['kind'], i['kind'])}：{i['path']}")
    engine = _antivirus(proj, r.seq, not av, log)
    blocked = sum(1 for i in r.issues if i["kind"] == "blocked")
    if blocked:
        log(f"  ✗ 另有 {blocked} 个文件在收图时就被本机杀毒软件拦下，没有收进项目，已按高风险登记。")
    sec = risk.batch_summary(proj, r.seq)
    sec["av"] = engine
    for f in sec["files"]:
        mark = "✗" if f["level"] == "high" else "⚠" if f["level"] == "medium" else "·"
        log(f"  {mark} {reports.LEVEL[f['level']]}风险 [{f['sha'][:12]}] {f['path'].split(' / ')[-1]}——{f['details'][0]}")
    if any(f["level"] == "high" for f in sec["files"]):
        log("  不要双击运行这些文件，也不要和图纸放在同一个文件夹里用 CAD 打开。")
    rep = reports.receive_report(proj, r, sec)
    log(f"解包报告：{rep}")
    return {"seq": r.seq, "files": r.files, "new_pages": r.new_pages, "report": str(rep),
            "issues": len(r.issues), "risky": len(sec["files"]), "blocked": blocked}


def _antivirus(proj, seq, skip: bool, log: Log) -> str | None:
    """返回用到的杀毒软件名；跳过时返回 skipped，没有杀毒软件时返回 None。"""
    if skip:
        return "skipped"
    engine = antivirus.find_engine()
    if engine is None:
        log("  本机没有找到杀毒软件，只做了静态检查。")
        return None
    log(f"  正在用 {engine.name} 扫描新文件……")
    res = antivirus.scan_project(proj, seq, engine)
    if not res.ok:
        log(f"  ⚠ {res.message}")
    elif res.threats or res.vanished:
        log(f"  ✗ {engine.name} 发现 {len(res.threats) + len(res.vanished)} 个有问题的文件，详见问题中心。")
    else:
        log(f"  {engine.name}：{res.scanned} 个文件未发现病毒。")
    return engine.name


# ---------------- 识别 ----------------

def recognize_simulated(proj: Project, task: Path | None = None, log: Log = print) -> dict:
    from .executors import simulated
    task = Path(task) if task else recognition.make_task(proj, render=False, scope="unrecognized")
    if task is None:
        log("没有需要识别的页面。")
        return {"done": 0, "imported": 0}
    n = simulated.run(task)
    res = recognition.import_results(proj, task / "results")
    log(f"模拟执行器识别 {n} 页，导入 {res.imported} 页。" + (f"{len(res.errors)} 个文件未通过校验。" if res.errors else ""))
    return {"done": n, "imported": res.imported, "errors": len(res.errors)}


def prepare_claude(proj: Project, scope: str = "new", tier: str = "economy", model: str | None = None,
                   tiles: int | None = None, task: Path | None = None) -> dict | None:
    """生成任务包并估算花费（不调用 API）。没有要识别的页面时返回 None。"""
    from .executors import claude
    task = Path(task) if task else recognition.make_task(proj, render=True, scope=scope)
    if task is None:
        return None
    return {"task": str(task), **claude.estimate(task, tier, model, tiles)}


def prepare_codex(proj: Project, scope: str = "new", tier: str = "economy", model: str | None = None,
                  tiles: int | None = None, task: Path | None = None, workers: int | None = None) -> dict | None:
    """生成任务包并估算用时（不调用 Codex）。没有要识别的页面时返回 None。"""
    from .executors import codex
    task = Path(task) if task else recognition.make_task(proj, render=True, scope=scope)
    if task is None:
        return None
    return {"task": str(task), **codex.estimate(task, tier, model, tiles, workers or codex.DEFAULT_WORKERS)}


def estimate_text(est: dict) -> str:
    if est.get("executor") == "codex":
        return (f"待识别 {est['pages']} 页，{est['images']} 张图片，用 Codex（模型 {est['model']}，思考强度 {est['effort']}），"
                f"同时识别 {est['workers']} 页，预计约 {est['minutes']} 分钟。不另外花钱，占用你 ChatGPT 账号的 Codex 用量额度。")
    usd = f"约 ${est['estimated_usd']:.2f}" if est["estimated_usd"] is not None else "未知（模型不在价格表里）"
    return (f"待识别 {est['pages']} 页，{est['images']} 张图片，模型 {est['model']}，粗略估算输入 {est['input_tokens']} tokens、"
            f"输出 {est['output_tokens']} tokens，花费{usd}。")


def _finish_recognition(proj: Project, task: Path, r, usage_text: str, log: Log) -> dict:
    log(f"识别 {r.done} 页，跳过 {r.skipped} 页，失败 {len(r.failed)} 页；{usage_text}。")
    for f in r.failed[:5]:
        log(f"  ✗ {f['file']}：{f['error']}")
    res = recognition.import_results(proj, Path(task) / "results")
    log(f"导入 {res.imported} 页。" + (f"{len(res.errors)} 个文件未通过校验，详见 reports/识别结果导入问题.md" if res.errors else ""))
    return {"done": r.done, "failed": len(r.failed), "imported": res.imported, "task": str(task), "model": r.model}


def recognize_claude(proj: Project, task: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
                     tiles: int | None = None, workers: int = 4, log: Log = print, progress=None, client=None) -> dict:
    from .executors import claude
    try:
        r = claude.run(Path(task), tier, model, effort, tiles, workers, client=client, progress=progress, log_fn=log)
    except claude.FatalError as e:
        raise OpError(f"{e}。任务包已保留，配置好后可以续跑") from e
    cost = r.usage.cost(r.model)
    out = _finish_recognition(proj, task, r, f"实际用量 输入 {r.usage.input} / 输出 {r.usage.output} tokens"
                              + (f"，约 ${cost:.4f}" if cost is not None else ""), log)
    return {**out, "usd": cost}


def recognize_codex(proj: Project, task: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
                    tiles: int | None = None, workers: int | None = None, log: Log = print, progress=None,
                    runner=None) -> dict:
    """用本机的 Codex（ChatGPT 账号）识别。runner 只在测试里用，代替真的 Codex。"""
    from .executors import claude, codex
    try:
        r = codex.run(Path(task), tier, model, effort, tiles, workers or codex.DEFAULT_WORKERS, progress=progress,
                      log_fn=log, runner=runner)
    except claude.FatalError as e:
        raise OpError(f"{e}。任务包已保留，可以续跑") from e
    return _finish_recognition(proj, task, r, f"用量 输入 {r.usage.input} / 输出 {r.usage.output} tokens"
                               "（ChatGPT 账号的 Codex 额度，不另外计费）", log)


def codex_status(refresh: bool = False) -> dict:
    from .executors import codex
    return codex.status(refresh)


def has_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# ---------------- 计算、导出、扫描 ----------------

def build(proj: Project, as_of: int | None = None, log: Log = print) -> dict:
    st = state.build(proj, as_of)
    is_latest = st["as_of"] == max((b["seq"] for b in st["batches"]), default=0)
    sub = None if is_latest else f"截至批次{st['as_of']:02d}"
    out = proj.path("exports", "state.json") if is_latest else proj.path("reports", sub, "state.json")
    out.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    files = reports.write_all(proj, st, sub)
    crit = sum(1 for i in st["issues"] if i["level"] == "crit")
    warn = sum(1 for i in st["issues"] if i["level"] == "warn")
    log(f"截至批次 #{st['as_of']}：{len(st['sheets'])} 个图号，严重问题 {crit} 项，提醒 {warn} 项。")
    for i in st["issues"]:
        if i["level"] != "info":
            log(f"  {'●' if i['level'] == 'crit' else '○'} [{i['kind']}] {i['title']}")
    log(("历史状态报告（reports/" + sub + "）：" if sub else "报告：") + "、".join(p.name for p in files))
    return {"as_of": st["as_of"], "crit": crit, "warn": warn, "reports": str(proj.root / "reports")}


def export_(proj: Project, what: str, out: Path | None = None, log: Log = print) -> Path:
    st = state.build(proj, with_diff=False)
    if what == "current":
        p = export.export_current(proj, st, out)
        log(f"现行图纸已导出：{p}")
    else:
        p = export.export_mirror(proj, st, out)
        log(f"数据镜像已导出：{p}")
    return p


def scan(proj: Project, av: bool = True, log: Log = print) -> dict:
    n = risk.rescan_static(proj)
    log(f"静态检查完成（规则第 {risk.RULES_VERSION} 版）：{n} 个文件有发现。")
    if av:
        engine = antivirus.find_engine()
        if engine is None:
            log("本机没有找到杀毒软件（Windows 自带 Defender；其他系统可装 ClamAV），跳过杀毒扫描。")
        else:
            log(f"正在用 {engine.name} 扫描全部文件……")
            res = antivirus.scan_project(proj, None, engine)
            log(reports.scan_line({"batch_seq": 0, "engine": engine.name, "scanned_at": "", "ok": res.ok,
                                   "message": res.message, "files": res.scanned,
                                   "threats": len(res.threats) + len(res.vanished)}).strip() + "。")
    st = state.build(proj, with_diff=False)
    p = proj.path("reports", "文件安全检查.md")
    p.write_text(reports.security_report(st), encoding="utf-8")
    pending = [f for f in st["security"]["files"] if f["level"] != "low" and not f["decision"]]
    for f in pending:
        log(f"  {'✗' if f['level'] == 'high' else '⚠'} {f['level_word']}风险 [{f['key']}] {f['name']}——{f['findings'][0]}")
    log(f"报告：{p}" + (f"。{len(pending)} 个文件待你确认，在工作台问题中心或用 bidbench decide risk <编号> safe|dangerous" if pending else ""))
    return {"report": str(p), "pending": len(pending)}
