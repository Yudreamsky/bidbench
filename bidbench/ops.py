"""日常操作：收图、识别、智能分类、计算报告、导出、安全扫描。命令行和桌面工作台共用，输出通过 log 回调给人看。"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from collections.abc import Callable

from . import antivirus, appconfig, cadfile, dwg, export, ingest, recognition, reports, risk, state
from .store import Project, now_iso

Log = Callable[[str], None]
logger = logging.getLogger(__name__)


class OpError(Exception):
    """操作没法进行。消息直接给用户看。"""


class LimitReached(OpError):
    """AI 的用量额度暂时用完：做好的都留着，额度恢复后接着做。"""


def _stop_error(e: Exception, message: str) -> OpError:
    from .executors import claude
    return (LimitReached if isinstance(e, claude.LimitError) else OpError)(message)


# ---------------- 收图 ----------------

def receive(proj: Project, sources, date: str, name: str | None = None, passwords=(), note: str | None = None,
            av: bool = True, log: Log = print, progress=None, label: str | None = None, exclude=()) -> dict:
    """收一批图纸：sources 是一个压缩包、文件或文件夹，或者同时收到的几个文件（列表）。label、exclude 见 ingest.receive。"""
    srcs = [Path(s) for s in sources] if isinstance(sources, (list, tuple)) else [Path(sources)]
    r = ingest.receive(proj, srcs if len(srcs) > 1 else srcs[0], date, name, list(passwords), note, progress=progress,
                       log=log, label=label, exclude=exclude)
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
    read_cad(proj, log=log, progress=progress)
    rep = reports.receive_report(proj, r, sec)
    log(f"解包报告：{rep}")
    return {"seq": r.seq, "files": r.files, "new_pages": r.new_pages, "report": str(rep),
            "issues": len(r.issues), "risky": len(sec["files"]), "blocked": blocked}


CAD_WORKERS = max(1, min(8, (os.cpu_count() or 2) - 1))  # 同时读几个：各在自己的进程里读（dwgread 写出几百 MB，读它很费 CPU，开线程快不了）


def read_cad(proj: Project, again: bool = False, log: Log = print, progress=None, workers: int | None = None) -> dict:
    """读 DWG、DXF 的图签属性和文字（docs/图纸字段.md 9.7）。收图后自动读新收的和以前没读过的；again 时全部重读。
    workers 个进程同时读（默认 CAD_WORKERS），读好一个存一个，存进项目都在这个进程里做。"""
    with proj.connect() as con:
        rows = dwg.unread(con, again)
    out = {"files": 0, "sheets": 0, "failed": 0, "no_tool": 0}
    if not rows:
        return out
    tool = dwg.find_tool()
    if tool is None and any(ext == ".dwg" for _s, _p, ext, _o, _b in rows):
        n = sum(1 for r in rows if r[2] == ".dwg")
        out["no_tool"] = n
        log(f"  没有找到 LibreDWG，{n} 个 DWG 只按文件名暂定。装好以后运行“读 DWG”补读。")
        rows = [r for r in rows if r[2] != ".dwg"]
    if rows:
        log(f"读 DWG、DXF 的图签和文字：{len(rows)} 个文件……")
    for i, (row, (d, err, trace)) in enumerate(_read_cad_files(proj, rows, tool, workers or CAD_WORKERS, log)):
        sha, _stored, _ext, path, batch = row
        if trace:
            logger.error("读 DWG、DXF 意外出错：%s\n%s", sha[:12], trace)
        n, d, err = _save_cad(proj, sha, path, batch, d, err)
        out["files"] += 1
        out["sheets"] += n
        if err:
            out["failed"] += 1
            log(f"  ⚠ 读不出：{path.split(' / ')[-1]}——{err}")
        elif d is not None and d.partial:
            log(f"  ⚠ 只读出一部分：{path.split(' / ')[-1]}——{dwg.PARTIAL}")
        if progress:
            progress(i + 1, len(rows))
    if rows:
        log(f"  读了 {out['files']} 个文件，认出 {out['sheets']} 张图" + (f"，{out['failed']} 个读不出（按文件名暂定）" if out["failed"] else "") + "。")
    return out


def _read_cad_files(proj: Project, rows: list, tool: str | None, workers: int, log: Log):
    """读好一个交出一个 (row, _read_cad_file 的结果)，谁先读好先交。读图进程出了问题（比如打包版开不了进程），
    剩下的在这个进程里一个个读，一个不漏、一个不重。"""
    left = {row[0]: row for row in rows}
    workers = min(workers, len(rows))
    if workers > 1:
        pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
        try:
            futures = {pool.submit(_read_cad_file, proj, row, tool): row for row in rows}
            for fut in as_completed(futures):
                row = futures.pop(fut)  # 交出去就放掉：几千个文件读出的东西不全留在内存里
                res = fut.result()
                del left[row[0]]
                yield row, res
        except (BrokenProcessPool, OSError):
            logger.exception("同时读 DWG、DXF 的进程出了问题")
            log(f"  同时读的进程出了问题，剩下的 {len(left)} 个一个个读。")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)  # 中途停下（Ctrl+C、数据库出错）：没开始读的不再读
    for row in list(left.values()):
        yield row, _read_cad_file(proj, row, tool)


def _read_cad_file(proj: Project, row: tuple, tool: str | None):
    """读一个 DWG、DXF（多半在读图进程里跑，排错日志写不到这里）：返回 (读出的图, 读不出的原因, 意外出错时的错误堆栈)。"""
    sha, stored, ext, path, _batch = row
    try:
        d = dwg.read(proj.root / stored, ext) if ext == ".dxf" else _read_dwg(proj, proj.root / stored, sha, path, tool)
        return d, None, None
    except dwg.ReadError as e:
        return None, str(e), None
    except Exception as e:  # noqa: BLE001 - 一个文件出意外的错不能停下整批，也不能让下次每次都卡在它上面
        return None, f"读的时候出了意外的错（{type(e).__name__}），只能按文件名暂定", traceback.format_exc()


def _save_cad(proj: Project, sha: str, path: str, batch: int, d, err):
    """一个文件读的结果存进项目；分图、认图签出错也记成读不出。返回 (认出几张图, 读出的图, 读不出的原因)。"""
    try:
        with proj.connect() as con:
            return dwg.save(con, sha, path, batch, d, err, now_iso()), d, err
    except sqlite3.Error:
        raise  # 数据库本身的问题（被占用、磁盘满）不是这个文件的错，停下来
    except Exception as e:  # noqa: BLE001 - 分图、认图签出错：同样记成读不出，接着读下一个
        logger.exception("读出的 DWG、DXF 分图出错：%s", sha[:12])
        err = f"读出来了但分图时出了意外的错（{type(e).__name__}），只能按文件名暂定"
        with proj.connect() as con:
            return dwg.save(con, sha, path, batch, None, err, now_iso()), None, err


def _read_dwg(proj: Project, src: Path, sha: str, name: str, tool: str | None) -> dwg.Drawing:
    """dwgread 读到一半崩了（docs/图纸字段.md 9.7 第 5 条）：先转成 DXF 读（转好的留在 exports/dxf，翻模也用它），
    转不了、或转出的也不完整，再读两次 dwgread，用读到最多的那次。"""
    d = dwg.read_dwg(src, tool)
    if not d.partial:
        return d
    try:
        dxf = cadfile.to_dxf(proj, src, sha, name)
        if dwg.dxf_complete(dxf):
            full = dwg.read_dxf(dxf)
            full.reader = f"{d.reader or 'LibreDWG'} dwg2dxf"
            return full
        logger.debug("dwg2dxf 转出的 DXF 也不完整")
    except (cadfile.CadError, dwg.ReadError, OSError) as e:
        logger.debug("没读完的 DWG 转 DXF 也不行：%s", e)
    again = dwg.read_dwg(src, tool, tries=2)
    return again if not again.partial or dwg._amount(again) > dwg._amount(d) else d


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


LIVE_IMPORT_PAGES = 20    # 识别途中每识别这么多页导入一次
LIVE_IMPORT_SECONDS = 60  # 或者隔这么久导入一次


class LiveImport:
    """识别途中分批把识别好的页导入项目：工作台跟着刷新，中途停下（额度用完、出错、关掉）也不丢已识别的页。

    当作进度回调交给执行器：执行器在自己的主线程里逐页调用，导入也就在这个线程里做。"""

    def __init__(self, proj: Project, task: Path, progress=None, on_import=None):
        self.proj, self.results = proj, Path(task) / "results"
        self.progress, self.on_import = progress, on_import
        self.seen: set[str] = set()
        self.errors: list[dict] = []
        self.count = 0  # 已导入的页（内容没变的也算）
        self.pending, self.t = 0, time.monotonic()

    def __call__(self, n: int, total: int) -> None:
        if self.progress:
            self.progress(n, total)
        self.pending += 1
        if self.pending >= LIVE_IMPORT_PAGES or time.monotonic() - self.t >= LIVE_IMPORT_SECONDS:
            self.flush()

    def flush(self) -> None:
        self.pending, self.t = 0, time.monotonic()
        files = [f for f in recognition.result_files(self.results) if str(f) not in self.seen]
        if not files:
            return
        res = recognition.import_results(self.proj, self.results, files=files, report=False)
        self.seen |= {str(f) for f in files}
        self.errors += res.errors
        self.count += res.imported + res.unchanged
        if self.on_import:
            self.on_import(self.count)

    def finish(self) -> None:
        self.flush()
        if self.errors:
            recognition.write_import_report(self.proj, self.errors)

    def stopped(self, e: Exception) -> OpError:
        """整次识别停下了（额度用完、凭据不对等）：先把已识别的导入，再告诉用户。"""
        self.finish()
        got = f"已识别的 {self.count} 页已导入。" if self.count else ""
        return _stop_error(e, f"{e}。{got}任务包已保留，可以续跑")


def plan_recognition(proj: Project, scope: str = "new", executor: str = "codex", tier: str = "economy",
                     model: str | None = None, tiles: int | None = None, workers: int | None = None,
                     disciplines=None) -> dict | None:
    """估算：要识别多少页、分几步（第一步地图不看图；再重点图、建模主图、按需深入，见 recognition.STAGES），
    每步、每组要多久或多少钱；也给旧的按优先级分档（priorities）。disciplines 给了只算这几个专业的图。

    不生成任务包、不渲染图片，几千页也很快；开始识别时才只为选中的几步准备图片。没有要识别的页面时返回 None。"""
    from .executors import claude, codex
    sp = recognition.stage_plan(proj, scope, disciplines)
    rows = sp.pop("_rows")
    hints = sp.pop("_hints")
    if not rows:
        return None
    plan = recognition.priority_plan(rows)
    by_key: dict[str, list] = {}
    by_group: dict[str, list] = {}
    for r in rows:
        by_key.setdefault(recognition.priority_of(r["first_path"]), []).append(r)
        by_group.setdefault(recognition.stage_group(r, hints), []).append(r)
    by_stage = {k: [r for g in recognition.STAGE_GROUPS if g[1] == k for r in by_group.get(g[0], [])]
                for k, _lb, _d in recognition.STAGES}
    if executor == "codex":
        cfg = codex.TIERS[tier]
        tiles = cfg["tiles"] if tiles is None else tiles
        workers = workers or codex.DEFAULT_WORKERS
        per_page = codex.SECONDS_PER_PAGE * (1.5 if tiles > 1 else 1) / max(1, workers) / 60  # 分钟

        def cost(rs):
            return {"minutes": max(1, round(len(rs) * per_page))}
        head = {"executor": "codex", "scope": scope, "tier": tier, "pages": len(rows),
                "images": len(rows) * (2 + (tiles * tiles if tiles > 1 else 0)), "model": model or cfg["model"],
                "effort": cfg["effort"], "workers": workers, "minutes": max(1, round(len(rows) * per_page))}
    else:
        def cost(rs):
            return {"usd": claude.estimate_rows(rs, tier, model, tiles)["estimated_usd"]}
        head = {"executor": "claude", "scope": scope, "tier": tier, "pages": len(rows), **claude.estimate_rows(rows, tier, model, tiles)}
    for g in plan:
        g.update(cost(by_key[g["key"]]))
    for st in sp["stages"]:
        if st["left"]:
            st.update(cost(by_stage[st["key"]]))
        for g in st["groups"]:
            g.update(cost(by_group[g["key"]]))
    return {**head, "priorities": plan, "map": sp["map"], "stages": sp["stages"]}


def duration_text(minutes: float) -> str:
    return f"约 {round(minutes)} 分钟" if minutes < 90 else f"约 {minutes / 60:.1f} 小时"


def prepare_claude(proj: Project, scope: str = "new", tier: str = "economy", model: str | None = None,
                   tiles: int | None = None, task: Path | None = None, stages: list[str] | None = None,
                   disciplines=None) -> dict | None:
    """生成任务包并估算花费（不调用 API）；stages 给了只放分步识别的这几步，disciplines 给了只放这几个专业的图。
    没有要识别的页面时返回 None。"""
    from .executors import claude
    task = Path(task) if task else recognition.make_task(proj, render=True, scope=scope, stages=stages, disciplines=disciplines)
    if task is None:
        return None
    return {"task": str(task), **claude.estimate(task, tier, model, tiles)}


def prepare_codex(proj: Project, scope: str = "new", tier: str = "economy", model: str | None = None,
                  tiles: int | None = None, task: Path | None = None, workers: int | None = None,
                  stages: list[str] | None = None, disciplines=None) -> dict | None:
    """生成任务包并估算用时（不调用 Codex）；stages 给了只放分步识别的这几步，disciplines 给了只放这几个专业的图。
    没有要识别的页面时返回 None。"""
    from .executors import codex
    task = Path(task) if task else recognition.make_task(proj, render=True, scope=scope, stages=stages, disciplines=disciplines)
    if task is None:
        return None
    return {"task": str(task), **codex.estimate(task, tier, model, tiles, workers or codex.DEFAULT_WORKERS)}


def estimate_text(est: dict) -> str:
    if est.get("executor") == "codex":
        text = (f"待识别 {est['pages']} 页，{est['images']} 张图片，用 Codex（模型 {est['model']}，思考强度 {est['effort']}），"
                f"同时识别 {est['workers']} 页，预计{duration_text(est['minutes'])}。不另外花钱，占用你 ChatGPT 账号的 Codex 用量额度。")
    else:
        usd = f"约 ${est['estimated_usd']:.2f}" if est["estimated_usd"] is not None else "未知（模型不在价格表里）"
        text = (f"待识别 {est['pages']} 页，{est['images']} 张图片，模型 {est['model']}，粗略估算输入 {est['input_tokens']} tokens、"
                f"输出 {est['output_tokens']} tokens，花费{usd}。")
    if est.get("stages"):
        return text + "\n" + stages_text(est)
    for i, g in enumerate(est.get("priorities") or [], 1):
        text += f"\n  {i}. {g['label']} {g['pages']} 页（{_cost_text(g)}），例如：{'、'.join(g['examples'])}"
    return text


def _cost_text(g: dict) -> str:
    return duration_text(g["minutes"]) if "minutes" in g else (f"约 ${g['usd']:.2f}" if g.get("usd") is not None else "")


STEP_NAMES = ("第一步", "第二步", "第三步", "第四步", "第五步")


def map_text(m: dict) -> str:
    """第一步（地图）做到哪了：只靠文件名、PDF 文字、DWG，不看图。"""
    text = (f"{'、'.join(m['disciplines'])}的图 " if m.get("disciplines") else "") + \
        f"{m['pages']} 页 PDF 里，{m['numbered']} 页从文件名或 PDF 文字认出了图号"
    if m["dwg_files"]:
        text += f"；DWG {m['dwg_files']} 个，读了 {m['dwg_read']} 个，读出 {m['dwg_sheets']} 张图"
    if m.get("skipped"):
        text += f"；你指定不用看图识别的 {m['skipped']} 页不算在下面几步里"
    return text + "。"


def stages_text(est: dict) -> str:
    """分步识别写成几行：每步几页、识别了几页、还要多久，举几个例子。"""
    lines = [f"分步识别（一步一步来，随时可以停，下次接着识别没做完的）：",
             f"  {STEP_NAMES[0]} 地图（不看图、不花钱）：{map_text(est['map'])}"]
    for i, st in enumerate(est["stages"], 1):
        done = f"，已识别 {st['done']} 页" if st["done"] else ""
        if not st["left"]:
            lines.append(f"  {STEP_NAMES[i]} {st['label']}：{st['total']} 页，都识别过了。" if st["total"] else
                         f"  {STEP_NAMES[i]} {st['label']}：没有这类页面。")
            continue
        lines.append(f"  {STEP_NAMES[i]} {st['label']}：还要识别 {st['left']} 页（{_cost_text(st)}）{done}——{st['desc']}")
        for g in st["groups"]:
            lines.append(f"      {g['label']} {g['pages']} 页，例如：{'、'.join(g['examples'])}")
    return "\n".join(lines)


def _finish_recognition(proj: Project, task: Path, r, usage_text: str, log: Log, live: LiveImport) -> dict:
    log(f"识别 {r.done} 页，跳过 {r.skipped} 页，失败 {len(r.failed)} 页；{usage_text}。")
    for f in r.failed[:5]:
        log(f"  ✗ {f['file']}：{f['error']}")
    live.finish()
    log(f"导入 {live.count} 页。" + (f"{len(live.errors)} 个文件未通过校验，详见 reports/识别结果导入问题.md" if live.errors else ""))
    return {"done": r.done, "failed": len(r.failed), "later": r.later, "imported": live.count, "task": str(task),
            "model": r.model}


def recognize_claude(proj: Project, task: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
                     tiles: int | None = None, workers: int = 4, log: Log = print, progress=None, client=None,
                     on_import=None, stop=None) -> dict:
    """on_import(已导入页数)：识别途中每导入一批调用一次。stop() 返回真就不再开始新的页（留着下次续跑）。"""
    from .executors import claude
    live = LiveImport(proj, task, progress, on_import)
    try:
        r = claude.run(Path(task), tier, model, effort, tiles, workers, client=client, progress=live, log_fn=log,
                       stop=stop)
    except claude.FatalError as e:
        raise live.stopped(e) from e
    except BaseException:
        live.finish()  # 意外出错、按了 Ctrl+C：已识别的先导入
        raise
    cost = r.usage.cost(r.model)
    out = _finish_recognition(proj, task, r, f"实际用量 输入 {r.usage.input} / 输出 {r.usage.output} tokens"
                              + (f"，约 ${cost:.4f}" if cost is not None else ""), log, live)
    return {**out, "usd": cost}


def recognize_codex(proj: Project, task: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
                    tiles: int | None = None, workers: int | None = None, log: Log = print, progress=None,
                    runner=None, on_import=None, stop=None) -> dict:
    """用本机的 Codex（ChatGPT 账号）识别。runner 只在测试里用，代替真的 Codex；on_import、stop 同 recognize_claude。"""
    from .executors import claude, codex
    live = LiveImport(proj, task, progress, on_import)
    try:
        r = codex.run(Path(task), tier, model, effort, tiles, workers or codex.DEFAULT_WORKERS, progress=live,
                      log_fn=log, runner=runner, stop=stop)
    except claude.FatalError as e:
        raise live.stopped(e) from e
    except BaseException:
        live.finish()
        raise
    return _finish_recognition(proj, task, r, f"用量 输入 {r.usage.input} / 输出 {r.usage.output} tokens"
                               "（ChatGPT 账号的 Codex 额度，不另外计费）", log, live)


# ---------------- 智能分类 ----------------

CLASSIFY_BY = {"codex": "Codex", "claude": "Claude", "simulated": "演示"}


def classify(proj: Project, executor: str = "codex", model: str | None = None, redo: bool = False, log: Log = print,
             progress=None, runner=None, client=None) -> dict:
    """智能分类：codex、claude 让 AI 分；simulated 按规则分（演示、测试用）。runner、client 只在测试里用。"""
    from . import classify as cls
    from .executors import claude, codex
    usage = claude.Usage()
    if executor == "codex":
        if runner is None:
            st = codex_status()
            if not st["ready"]:
                raise OpError(st["hint"] or "Codex 还不能用")
        model = model or codex.DEFAULT_MODEL

        def answer(kind, items, prompt):
            return codex.ask_json(prompt, cls.SCHEMAS[kind], model, cls.EFFORT, runner=runner, usage=usage,
                                  resume="再分一次，已分好的不用重分")
    elif executor == "claude":
        if client is None:
            if not has_claude_credentials():
                raise OpError("还没有设置 Claude API 密钥，请先到“设置”里填写")
            import anthropic
            client = anthropic.Anthropic(max_retries=4)
        model = model or cls.CLAUDE_MODEL

        def answer(kind, items, prompt):
            return claude.ask_json(client, prompt, cls.SCHEMAS[kind], cls.SYSTEM, model, cls.EFFORT, usage=usage)
    elif executor == "simulated":
        model = None

        def answer(kind, items, prompt):
            return cls.rule_body(kind, items)
    else:
        raise OpError("未知的分类方式")
    who = "按规则演示，不调用 AI" if executor == "simulated" else f"用 {CLASSIFY_BY[executor]}（{model}）"
    log(f"智能分类：{who}，{'全部重新分' if redo else '只分还没分过的'}。")
    try:
        r = cls.run(proj, answer, executor, model, redo, log, progress)
    except claude.FatalError as e:
        raise _stop_error(e, f"智能分类停下了：{e}") from e
    finally:
        if usage.input or usage.output:
            cost = usage.cost(model) if executor == "claude" else None
            log(f"用量：输入 {usage.input} / 输出 {usage.output} tokens"
                + (f"，约 ${cost:.4f}" if cost is not None else "（ChatGPT 账号的 Codex 额度，不另外计费）" if executor == "codex" else ""))
    return r


CLASSIFY_EVERY = 200  # 识别途中每导入这么多页，在后台给新出现的图纸和事实分一次类


class ClassifyAlong:
    """边识别边分类：当作 on_import 回调交给识别。途中每导入 CLASSIFY_EVERY 页，在后台给新出现的图纸和事实分一次类
    （上一次分完才开始下一次）；识别完再分一次。只在用 Codex、Claude 识别时分；分不成不影响识别结果。"""

    def __init__(self, proj: Project, executor: str, model: str | None = None, log: Log = print, progress=None):
        self.proj, self.executor, self.log, self.progress = proj, executor, log, progress
        self.model = model if executor == "codex" else None  # Claude 识别选的模型不一定适合分类，用分类的默认模型
        self.on = executor in ("codex", "claude")
        self.thread: threading.Thread | None = None
        self.last = 0

    def __call__(self, imported: int) -> None:
        if not self.on or imported - self.last < CLASSIFY_EVERY or (self.thread and self.thread.is_alive()):
            return
        self.last = imported
        self.thread = threading.Thread(target=self._quietly, daemon=True, name="classify-along")
        self.thread.start()

    def _quietly(self) -> None:
        try:
            r = classify(self.proj, self.executor, self.model, log=lambda s: None)
        except Exception as e:  # noqa: BLE001 - 途中分不成没关系，识别完还会再分一次
            logger.info("边识别边分类没有完成：%s", type(e).__name__)
            return
        if r["sheets"] or r["facts"]:
            self.log(f"  · 边识别边分类：分好 {r['sheets']} 张图、{r['facts']} 类事实，左边跟着更新")

    def wait(self) -> None:
        if self.thread:
            self.thread.join()

    def finish(self) -> None:
        """识别完：等后台那次分完，再把剩下的分完。"""
        self.wait()
        if not self.on:
            return
        try:
            classify(self.proj, self.executor, self.model, log=self.log, progress=self.progress)
        except OpError as e:
            self.log(f"智能分类没有完成：{e}。稍后可以再点“智能分类”（命令行：bidbench classify）。")


def classify_if_ready(proj: Project, log: Log = print) -> dict | None:
    """收图后：用过 Codex、Claude 识别（设置里记着识别方式）而且现在能用，就按名字给新图纸智能分类（只发名字，几分钟）。"""
    pref = appconfig.prefs().get("recognize") or {}
    ex = pref.get("executor")
    if ex == "codex" and not codex_status()["ready"] or ex == "claude" and not has_claude_credentials() \
            or ex not in ("codex", "claude"):
        return None
    try:
        return classify(proj, ex, pref.get("model") if ex == "codex" else None, log=log)
    except OpError as e:
        log(f"智能分类没有完成：{e}。稍后可以再点“智能分类”（命令行：bidbench classify）。")
        return None


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
    if is_latest and st["guide"]["tasks"]:  # 和工作台问题中心最上面的“下一步”一样
        log(f"下一步：{st['guide']['tasks'][0]['title']}")
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
