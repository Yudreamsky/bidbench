"""智能收图：给一级目录（比如整个共享盘），判断收哪些子文件夹、分几批。

1. 只列文件夹、不读文件内容：每个文件夹（连同下面各层）有几个 PDF、DWG、压缩包、Office、邮件……多大。
2. 判断收哪些：先按文件夹名字和数量（有图纸、名字不像邮件会议照片备份的）；用 Codex 时只把文件夹名字、
   各类文件的数量和几个图纸文件名交给它，由它判断哪些是图纸、深入到哪一层、下面哪些不要。
3. 排成几批：放得下（不超过收图上限）的文件夹整个一批，放不下的往下拆。写成收图计划（项目 reports 里的 md 和 json），
   看过再按计划一批批收；断了再收一次，收过的批次自动跳过，复制进项目的文件不再从网络读。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ingest
from .store import Project

logger = logging.getLogger(__name__)

EMAIL_EXTS = (".msg", ".eml", ".pst", ".mbox")
DRAWING_KINDS = ("pdf", "cad")
KIND_LABEL = {"pdf": "PDF", "cad": "DWG/DXF", "archive": "压缩包", "office": "Office", "image": "图片", "email": "邮件",
              "text": "文本", "other": "其他"}
NOT_DRAWINGS = re.compile(r"e-?mail|邮件|\bmom\b|meeting|会议|minutes|photo|照片|相片|video|视频|recycle|回收站|"
                          r"backup|备份|\btemp\b|\btmp\b|~snapshot|#recycle", re.I)
NOTE_EVERY = 5000    # 每看过这么多个文件报一次数
OUTLINE_DEPTH = 3    # 交给 AI 看到第几层
OUTLINE_MAX = 400    # 交给 AI 的文件夹最多这么多个（图纸多的优先）
CODEX_MODEL = None   # 用 Codex 的默认模型


def kind_of(name: str) -> str:
    ext = ingest._ext_of(name)
    if ext in EMAIL_EXTS:
        return "email"
    if ext in ingest.ARCHIVE_EXTS:
        return "archive"
    return ingest.KIND_BY_EXT.get(ext, "other")


@dataclass
class Folder:
    rel: str                     # 相对一级目录的路径，用 / 分开；一级目录本身是 ""
    path: Path
    kinds: Counter = field(default_factory=Counter)   # 这个文件夹连同下面各层，按类型数的文件
    bytes: int = 0
    direct: int = 0              # 直接放在这一层的文件
    direct_bytes: int = 0
    samples: list = field(default_factory=list)       # 几个图纸文件名（给 AI 看）
    children: list = field(default_factory=list)
    unreadable: bool = False

    @property
    def files(self) -> int:
        return sum(self.kinds.values())

    @property
    def drawings(self) -> int:
        return sum(self.kinds[k] for k in DRAWING_KINDS) + self.kinds["archive"]

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1] if self.rel else ingest.folder_label(Path(ingest._unlong(self.path)))


def scan(top: Path, log=None, skip: str | None = None) -> Folder:
    """只列文件夹、不读文件：用列目录时一起拿到的信息数文件、算大小，网络文件夹上每个文件夹来回一趟。"""
    root = Folder("", ingest._long(top))
    order, stack, seen = [root], [root], 0
    while stack:
        node = stack.pop()
        try:
            with os.scandir(node.path) as it:
                items = sorted(it, key=lambda e: e.name)
        except OSError:
            node.unreadable = True
            continue
        for e in items:
            try:
                if e.is_symlink() or getattr(e, "is_junction", lambda: False)():
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if skip is None or ingest._plain(Path(e.path)) != skip:
                    node.children.append(Folder(f"{node.rel}/{e.name}".lstrip("/"), Path(e.path)))
                continue
            if ingest._is_noise(e.name):
                continue
            try:
                size = e.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0
            k = kind_of(e.name)
            node.kinds[k] += 1
            node.direct += 1
            node.direct_bytes += size
            if k in DRAWING_KINDS and len(node.samples) < 3:
                node.samples.append(e.name)
            seen += 1
            if log and seen % NOTE_EVERY == 0:
                log(f"  正在列文件夹：看过 {len(order)} 个文件夹、{seen} 个文件……")
        order.extend(node.children)
        stack.extend(reversed(node.children))
    for node in reversed(order):  # 子文件夹都排在上一层后面：倒着加，下面各层的数就加到了上面
        node.bytes += node.direct_bytes
        for c in node.children:
            node.kinds.update(c.kinds)
            node.bytes += c.bytes
    if log:
        log(f"  列完了：{len(order)} 个文件夹、{root.files} 个文件，共 {root.bytes / 1024**3:.1f} GB，"
            f"其中 PDF {root.kinds['pdf']}、DWG/DXF {root.kinds['cad']}、压缩包 {root.kinds['archive']}。")
    return root


def _walk(node: Folder):
    yield node
    for c in node.children:
        yield from _walk(c)


def outline(root: Folder, depth: int = OUTLINE_DEPTH, limit: int = OUTLINE_MAX) -> list[Folder]:
    """交给 AI 看的文件夹：前几层里有文件的，太多时留图纸多的（一级目录下面的一层全留）。"""
    rows = [f for f in _walk(root) if f.rel and f.rel.count("/") < depth and f.files]
    if len(rows) > limit:
        keep = {f.rel for f in rows if "/" not in f.rel}
        keep |= {f.rel for f in sorted(rows, key=lambda f: -f.drawings)[:max(limit - len(keep), 0)]}
        rows = [f for f in rows if f.rel in keep]
    return rows


def _line(i: int, f: Folder) -> str:
    counts = "、".join(f"{KIND_LABEL[k]} {n}" for k, n in sorted(f.kinds.items(), key=lambda kv: -kv[1]) if n)
    eg = f"；例：{'、'.join(f.samples)}" if f.samples else ""
    return f"{i}|{f.rel}|{counts}；共 {f.bytes / 1024**3:.2f} GB{eg}"


SCHEMA = {
    "type": "object",
    "properties": {
        "include": {"type": "array", "items": {"type": "object", "properties": {
            "i": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["i", "reason"]}},
        "exclude": {"type": "array", "items": {"type": "object", "properties": {
            "i": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["i", "reason"]}},
    },
    "required": ["include", "exclude"],
}


def prompt(rows: list[Folder]) -> str:
    return "\n".join([
        "下面是一个工程项目共享盘的文件夹目录（只有文件夹名字、每个文件夹连同下面各层里各类文件的数量和几个图纸文件名，"
        "没有文件内容）。要收进图纸管理工具、做识别和建模的是图纸：施工图、招标图、深化图、竣工图、变更和答疑里的图，"
        "以及图纸目录、各种表。邮件、会议纪要、材料报审、照片视频、合同报表、备份副本不要。",
        "判断收哪些文件夹：include 列要收的文件夹编号（收它就连下面各层一起收，能整个收的就别拆开）；"
        "exclude 列在要收的文件夹下面、但不要的子文件夹编号。只按名字和数量判断，每条写一句理由。",
        "每行：编号|路径|各类文件数量；大小；例：图纸文件名",
        *(_line(i, f) for i, f in enumerate(rows, 1)),
    ])


def rule_pick(root: Folder) -> tuple[dict[str, str], dict[str, str]]:
    """不用 AI 的判断：一级目录下面每个有图纸、名字不像邮件会议照片备份的文件夹整个收；下面名字像的不收。"""
    include, exclude = {}, {}
    for f in root.children:
        if f.drawings and not NOT_DRAWINGS.search(f.name):
            include[f.rel] = f"有 PDF、DWG 或压缩包 {f.drawings} 个"
            for g in _walk(f):
                if g is not f and NOT_DRAWINGS.search(g.name):
                    exclude[g.rel] = "名字像邮件、会议、照片或备份"
    if not include and root.drawings and root.direct:
        include[""] = "一级目录里直接放着图纸"
    return include, exclude


def ai_pick(root: Folder, ask) -> tuple[dict[str, str], dict[str, str]]:
    """ask(提示, 格式) 返回 AI 的回答（dict）。编号对不上、选了不在目录里的都丢掉。"""
    rows = outline(root)
    body = ask(prompt(rows), SCHEMA)
    by_i = {i: f for i, f in enumerate(rows, 1)}

    def pick(key):
        out = {}
        for x in body.get(key) or []:
            f = by_i.get(x.get("i")) if isinstance(x, dict) else None
            if f is not None:
                out[f.rel] = str(x.get("reason") or "").strip().rstrip("。.；;")[:200]
        return out
    include, exclude = pick("include"), pick("exclude")
    for rel in list(include):  # 已经被上一层整个收进来的不用再列
        if any(rel.startswith(o + "/") for o in include if o != rel):
            include.pop(rel)
    exclude = {rel: why for rel, why in exclude.items() if any(rel.startswith(o + "/") for o in include)}
    return include, exclude


def plan(root: Folder, include: dict[str, str], exclude: dict[str, str]) -> list[dict]:
    """排成几批：每批是一个文件夹（连同下面各层，去掉不要的子文件夹），放不下（超过收图上限）就往下拆，
    子文件夹各一批，这一层直接放的文件另成一批。"""
    by_rel = {f.rel: f for f in _walk(root)}
    top = ingest.folder_label(Path(ingest._unlong(root.path)))
    out = []

    def below(rel: str, node: Folder) -> bool:
        return rel != node.rel and (not node.rel or rel.startswith(node.rel + "/"))

    def batch(node: Folder, ex: list[str], why: str, direct_only: bool = False) -> dict:
        ex = [x for x in ex if not any(x != y and x.startswith(y + "/") for y in ex)]  # 上一层已经不收的不用再列
        files = node.direct if direct_only else node.files - sum(by_rel[x].files for x in ex)
        size = node.direct_bytes if direct_only else node.bytes - sum(by_rel[x].bytes for x in ex)
        label = " › ".join(node.rel.split("/")) if node.rel else top
        return {"rel": node.rel, "path": ingest._unlong(node.path),
                "label": label + ("（这一层直接放的文件）" if direct_only else ""),
                "exclude": sorted(x[len(node.rel) + 1:] if node.rel else x for x in ex),
                "files": files, "bytes": size, "reason": why, "date": date_in(node.rel),
                "too_big": files > ingest.MAX_SOURCE_FILES or size > ingest.MAX_TOTAL_BYTES}

    def take(node: Folder, why: str) -> None:
        if node.rel in exclude:
            return
        b = batch(node, [x for x in exclude if below(x, node)], why)
        if not b["files"]:
            return
        if not b["too_big"] or not node.children:
            out.append(b)
            return
        for c in node.children:
            take(c, why)
        if node.direct:
            out.append(batch(node, [c.rel for c in node.children], why, direct_only=True))

    def find(node: Folder) -> None:
        if node.rel in include:
            take(node, include[node.rel])
            return
        for c in node.children:
            find(c)
    find(root)
    return in_order(out)


DATE = re.compile(r"(?<!\d)(20\d\d)[-._]?(0[1-9]|1[0-2])[-._]?(0[1-9]|[12]\d|3[01])(?!\d)")
TENDER = re.compile(r"tender|招标|投标|bid", re.I)


def date_in(rel: str) -> str | None:
    """文件夹名字里的日期（20240308、2024-03-08、2024.03.08），最里面一层的为准：一次次接收图纸的文件夹常这样起名。"""
    for part in reversed(rel.split("/")):
        found = DATE.findall(part)
        if found:
            y, m, d = found[-1]
            return f"{y}-{m}-{d}"
    return None


def in_order(batches: list[dict]) -> list[dict]:
    """按先来后到排：招标图在最前，名字里有日期的按日期，其余（汇总、深化等现行的整套）放最后。
    先收的批次算早到：现行整套要是先收，后面历次接收的旧版本都会被当成“后收到的反而是旧版”。"""
    first = [b for b in batches if not b["date"] and TENDER.search(b["label"])]
    dated = sorted((b for b in batches if b["date"]), key=lambda b: b["date"])
    rest = [b for b in batches if not b["date"] and b not in first]
    return first + dated + rest


def make_plan(proj: Project, top: Path, executor: str = "codex", model: str | None = None, log=print,
              runner=None) -> dict:
    """列文件夹、判断、排批次，写进项目 reports；返回计划。executor 是 codex 或 rules（只按名字和数量）。"""
    top = Path(top)
    if not top.is_dir():
        raise ValueError(f"找不到文件夹 {top}")
    log(f"智能收图：列一级目录 {top}（只列文件夹，不读文件内容）……")
    root = scan(top, log, skip=ingest._plain(proj.root.resolve()))
    if not root.files:
        raise ValueError("这个文件夹里没有可收的文件")
    how = "按文件夹名字和数量"
    if executor == "codex":
        from . import ops
        from .executors import codex
        if runner is None and not ops.codex_status()["ready"]:
            log("  Codex 现在用不了，先按文件夹名字和数量判断。")
            include, exclude = rule_pick(root)
        else:
            def ask(text, schema):
                return codex.ask_json(text, schema, model or CODEX_MODEL, "medium", runner=runner,
                                      resume="再做一次智能收图")
            log(f"  请 Codex 判断收哪些文件夹（只发文件夹名字、各类文件数量和几个图纸文件名）……")
            try:
                include, exclude = ai_pick(root, ask)
                how = "Codex 按文件夹名字和数量"
            except Exception as e:  # noqa: BLE001 - AI 判断不成：照规则来，计划照样出
                logger.info("智能收图：Codex 判断没成：%s", type(e).__name__)
                log(f"  Codex 没判断成（{e}），先按文件夹名字和数量判断。")
                include, exclude = rule_pick(root)
            if not include:
                log("  Codex 一个文件夹也没选，改按文件夹名字和数量判断。")
                include, exclude = rule_pick(root)
                how = "按文件夹名字和数量"
    else:
        include, exclude = rule_pick(root)
    batches = plan(root, include, exclude)
    skipped = [{"rel": f.rel, "files": f.files, "drawings": f.drawings, "bytes": f.bytes}
               for f in root.children if f.rel not in include and not any(r.startswith(f.rel + "/") for r in include)]
    out = {"created": datetime.now().isoformat(timespec="seconds"), "top": ingest._unlong(root.path), "how": how,
           "total": {"files": root.files, "bytes": root.bytes, "pdf": root.kinds["pdf"], "cad": root.kinds["cad"]},
           "include": include, "exclude": exclude, "batches": batches, "not_taken": skipped}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = proj.path("reports", f"收图计划-{stamp}.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    md = path.with_suffix(".md")
    md.write_text(plan_text(out), encoding="utf-8")
    out["file"] = str(path)
    log(plan_text(out, short=True))
    log(f"收图计划：{md}")
    return out


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{max(1, round(n / 1024**2))} MB"


def plan_text(p: dict, short: bool = False) -> str:
    t = p["total"]
    files = sum(b["files"] for b in p["batches"])
    size = sum(b["bytes"] for b in p["batches"])
    lines = [f"一级目录 {p['top']}：{t['files']} 个文件（{_gb(t['bytes'])}），PDF {t['pdf']}、DWG/DXF {t['cad']}。",
             f"{p['how']}判断，收 {len(p['batches'])} 批、{files} 个文件（{_gb(size)}）："]
    for i, b in enumerate(p["batches"], 1):
        ex = f"；不收 {'、'.join(b['exclude'])}" if b["exclude"] else ""
        big = "（还是超过上限，收不下）" if b["too_big"] else ""
        when = f"，{b['date']}" if b.get("date") else ""
        lines.append(f"  第 {i} 批 {b['label']}：{b['files']} 个文件（{_gb(b['bytes'])}{when}）{big}——{b['reason']}{ex}")
    if p["not_taken"]:
        lines.append("不收：" + "、".join(f"{x['rel']}（{x['files']} 个文件）" for x in p["not_taken"][:30])
                     + ("……" if len(p["not_taken"]) > 30 else ""))
    if not short:
        lines += ["", "先后：招标图在最前，名字里有日期的按日期，汇总、深化等整套放最后；名字里有日期的批次用这个日期当收图日期。",
                  "按这个计划收：bidbench receive-plan <这个计划的 json 文件> --date 收图日期"]
    return "\n".join(lines)


def receive_plan(proj: Project, plan_or_path, date: str, av: bool = True, log=print, progress=None,
                 only: list[int] | None = None) -> dict:
    """按计划一批批收（only 给了就只收这几批，从 1 数）。收过的批次跳过，一批出错记下来接着收下一批。
    文件夹名字里有日期的批次用那个日期当收图日期，其余用 date。"""
    from . import ops
    p = plan_or_path if isinstance(plan_or_path, dict) else json.loads(Path(plan_or_path).read_text(encoding="utf-8"))
    todo = [(i, b) for i, b in enumerate(p["batches"], 1) if only is None or i in only]
    out = {"received": [], "skipped": [], "failed": []}
    t0 = time.monotonic()
    for n, (i, b) in enumerate(todo, 1):
        log(f"—— 第 {i} 批（{n}/{len(todo)}）：{b['label']}，{b['files']} 个文件（{_gb(b['bytes'])}）——")
        try:
            r = ops.receive(proj, Path(b["path"]), b.get("date") or date, name=b["label"], av=av, log=log,
                            progress=progress, label=b["label"], exclude=b.get("exclude") or ())
            out["received"].append({"batch": i, "seq": r["seq"], "files": r["files"]})
        except ValueError as e:
            if "已经收过了" in str(e):
                log(f"  这一批以前收过了，跳过：{e}")
                out["skipped"].append({"batch": i, "reason": str(e)})
            else:
                log(f"  ✗ 这一批没收成：{e}")
                out["failed"].append({"batch": i, "error": str(e)})
        except OSError as e:
            log(f"  ✗ 这一批没收成（{type(e).__name__}: {e}），再按计划收一次会接着收")
            out["failed"].append({"batch": i, "error": f"{type(e).__name__}: {e}"})
    log(f"按计划收图完成：收了 {len(out['received'])} 批，以前收过 {len(out['skipped'])} 批，"
        f"没收成 {len(out['failed'])} 批，用时 {(time.monotonic() - t0) / 60:.0f} 分钟。")
    return out
