"""操作日志：什么时候、谁、做了什么、结果怎样。

- 收图、识别、导出、扫描等操作（命令行和工作台都算）写在项目的 logs/ 文件夹里，按月一个文件，每行一条，只追加不改写。
  日志跟着项目走：项目拷到别的电脑，日志也在。
- 人工决定、锁定基线、AI 助手的提议和处理结果本来就记在数据库里，查看时合并进来，不重复记录。

排错日志（给开发用）在 debuglog.py，不在这里。
"""

from __future__ import annotations

import getpass
import json
import logging
from datetime import datetime
from pathlib import Path

from . import guide
from .store import REVOKE, Project, now_iso

FOLDER = "logs"
MAX_LINES = 400
STATUS_WORD = {"done": "完成", "error": "出错", "interrupted": "中断"}
TITLES = {"estimate": "识别估算", "simulated": "模拟识别", "claude": "Claude 识别", "codex": "Codex 识别", "build": "重新计算报告",
          "current": "导出现行图纸", "mirror": "导出数据镜像", "scan": "文件安全扫描", "tasks": "生成识别任务包",
          "import": "导入识别结果", "classify": "智能分类", "read_dwg": "读 DWG", "overnight": "整夜识别",
          "survey": "智能收图规划", "receive_plan": "按计划收图"}

log = logging.getLogger(__name__)


def folder(proj: Project) -> Path:
    return proj.dir(FOLDER)


def receive_title(paths) -> str:
    from .ingest import folder_label
    paths = [Path(p) for p in paths]
    what = folder_label(paths[0]) + ("（文件夹）" if paths[0].is_dir() else "")
    return f"收图：{what}" + (f" 等 {len(paths)} 个文件" if len(paths) > 1 else "")


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - 取不到用户名不影响记录
        return ""


def _trim(lines) -> list[str]:
    lines = [str(x) for x in lines]
    if len(lines) <= MAX_LINES:
        return lines
    head = 40
    tail = MAX_LINES - head - 1
    return lines[:head] + [f"……中间省略 {len(lines) - head - tail} 行……"] + lines[-tail:]


def summary(kind: str, r) -> str | None:
    """一句话结果，显示在日志列表里。结果不完整时不写，不影响记录。"""
    try:
        return _summary(kind, r) if isinstance(r, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def _summary(kind: str, r: dict) -> str | None:
    usd = f"，约 ${r['usd']:.2f}" if r.get("usd") else ""
    if kind == "receive":
        extra = [f"解包问题 {r['issues']} 项" if r.get("issues") else "", f"风险文件 {r['risky']} 个" if r.get("risky") else ""]
        return f"批次 #{r['seq']}：{r['files']} 个文件，新增 {r['new_pages']} 页" + "".join(f"，{x}" for x in extra if x)
    if kind == "recognize":
        return f"识别 {r.get('done', 0)} 页" + (f"，失败 {r['failed']} 页" if r.get("failed") else "") + \
            f"，导入 {r.get('imported', 0)} 页" + usd
    if kind == "estimate":
        cost = f"，估算约 ${r['estimated_usd']:.2f}" if r.get("estimated_usd") is not None else ""
        minutes = f"，预计约 {r['minutes']} 分钟" if r.get("minutes") else ""
        return f"{r['pages']} 页{cost}{minutes}"
    if kind == "build":
        return f"截至批次 #{r['as_of']}：严重问题 {r['crit']} 项，提醒 {r['warn']} 项"
    if kind == "export":
        return r.get("path")
    if kind == "scan":
        return f"{r['pending']} 个风险文件待确认" if r.get("pending") else "没有待确认的风险文件"
    if kind == "assistant":
        return f"查询 {r.get('tools', 0)} 次" + (f"，生成提议 {r['proposals']} 条" if r.get("proposals") else "")
    if kind == "survey":
        return f"{len(r['batches'])} 批，{sum(x['files'] for x in r['batches'])} 个文件"
    if kind == "receive_plan":
        return f"收了 {len(r['received'])} 批" + (f"，以前收过 {len(r['skipped'])} 批" if r["skipped"] else "") + \
            (f"，没收成 {len(r['failed'])} 批" if r["failed"] else "")
    if kind == "overnight":
        return f"识别 {r['done']} 页" + (f"，失败 {r['failed']} 页" if r["failed"] else "") + \
            (f"，还剩 {r['left']} 页" if r.get("left") else "") + (f"；停在：{r['stopped']}" if r.get("stopped") else "")
    return None


def write(proj: Project, kind: str, title: str, *, source: str, status: str = "done", lines=(), error: str | None = None,
          started: str | None = None, seconds: float | None = None, result=None) -> dict:
    """记一条操作。写不进去（磁盘满、没有权限）时只记到排错日志，不影响操作本身。"""
    entry = {"time": started or now_iso(), "source": source, "user": _user(), "kind": kind, "title": title,
             "status": status, "summary": summary(kind, result) if status == "done" else None, "error": error,
             "seconds": round(seconds, 1) if seconds is not None else None, "lines": _trim(lines)}
    try:
        with open(folder(proj) / f"操作日志-{entry['time'][:7]}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("操作日志写不进去：%s", title)
    return entry


# ---------------- 查看 ----------------

def when(s: str) -> datetime:
    """记录里的时间换成本机时区的时间（不带时区的按本机时间算）。"""
    try:
        return datetime.fromisoformat(s).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime(1970, 1, 2).astimezone()


decision_text = guide.decision_text  # 决定写成人话：和问题中心“做过的决定”用同一份


def _from_db(proj: Project) -> list[dict]:
    from .assistant import CONFIRMED_ACTOR, _summary
    rows = []
    blank = {"source": "", "user": "", "status": "done", "error": None, "seconds": None, "lines": []}
    with proj.connect() as con:
        last: dict[tuple, str] = {}  # 每项最后一次的值：撤回时写撤回了什么
        for r in con.execute("SELECT * FROM decisions ORDER BY id"):
            if r["kind"] == REVOKE:
                try:
                    kind, key = json.loads(r["key"])
                except (ValueError, TypeError):
                    continue
                prev = last.pop((kind, key), None)
                what = decision_text(kind, key, prev) if prev is not None else f"{kind} {key}"
                rows.append({**blank, "time": r["created_at"], "actor": r["actor"], "kind": "decision",
                             "title": "撤回决定：" + what, "summary": r["note"]})
                continue
            last[(r["kind"], r["key"])] = r["value"]
            if r["actor"] == CONFIRMED_ACTOR:
                continue  # 确认提议那一条已经写了结果
            rows.append({**blank, "time": r["created_at"], "actor": r["actor"], "kind": "decision",
                         "title": "决定：" + decision_text(r["kind"], r["key"], r["value"]), "summary": r["note"]})
        for r in con.execute("SELECT * FROM proposals ORDER BY id"):
            what = _summary(r["kind"], json.loads(r["payload"]))
            rows.append({**blank, "time": r["created_at"], "source": r["source"], "kind": "proposal",
                         "title": f"提议 #{r['id']}：{what}", "summary": f"理由：{r['reason']}"})
            if r["decided_at"]:
                title = {"confirmed": "确认提议 #{}", "rejected": "驳回提议 #{}", "stale": "提议 #{} 已失效"}.get(
                    r["status"], "提议 #{} " + r["status"]).format(r["id"])
                rows.append({**blank, "time": r["decided_at"], "actor": "我", "kind": f"proposal_{r['status']}",
                             "title": title, "summary": r["outcome"]})
        for r in con.execute("SELECT * FROM baselines"):
            n = len(json.loads(r["snapshot"]))
            rows.append({**blank, "time": r["created_at"], "kind": "baseline", "title": f"锁定基线「{r['name']}」",
                         "summary": f"{n} 张图，截至批次 #{r['as_of_batch']}" + (f"；{r['note']}" if r["note"] else "")})
    return rows


def read(proj: Project, limit: int = 200) -> list[dict]:
    """最新的在前。"""
    rows = []
    d = proj.root / FOLDER
    for p in sorted(d.glob("操作日志-*.jsonl")) if d.is_dir() else []:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue  # 写到一半断电等，跳过这一行
            if isinstance(e, dict) and e.get("time") and e.get("title"):
                rows.append(e)
    rows += _from_db(proj)
    rows.sort(key=lambda e: when(e["time"]))  # 时间只精确到秒；同一秒内的保持记录先后，再整体倒过来
    rows.reverse()
    return rows[:limit]
