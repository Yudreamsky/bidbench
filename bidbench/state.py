"""派生状态：图纸台账、版本合并、批次性质、缺图、关系、事实层、问题清单。

输入只有两类：收到的批次（及其识别结果）和人的决定；另外套用 AI 的智能分类（只影响专业认不出的图和显示时的分组）。
派生结果每次重新计算，因此“截至第 N 批”的状态可以随时还原，差异报告就是相邻两批状态的比较。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import classify, floors, pdfpages, risk
from .ingest import FOLDER_MARK
from .recognition import weak_reason
from .store import PLACEHOLDER_EXECUTORS, Project, latest_decisions, now_iso

SAME_TEXT = 0.97      # 文字层相似度阈值：高于它且图形接近，视为重新导出的同一张图
SAME_DHASH = 10       # 差值哈希距离阈值（256 位）
FULL_REISSUE = 0.8    # 图号覆盖率达到它，判断为整套重发
GAP_MAX = 3           # 编号断号：同一百位段里连续缺这么多张以内才提示
COVER_LIST_MAX = 8    # 楼层覆盖：图多于这么多张时只写汇总的楼层范围，不逐张列
# 不分楼层、建哪一层都要看的“通用图”：设计说明，以及没写楼层的柱、剪力墙平面
GENERAL_SHEET = re.compile(r"说明|(?:柱|剪力墙|核心筒).{0,4}(?:平面|布置|定位)")
# 只对这张图有效的值（本层标高、适用楼层）：每张图各写各的，不跨图比较
SHEET_SCOPED = re.compile(r"^(?:本层|本图|本页|该层|此层|本张)|适用(?:楼层|范围)|楼层范围")

STATUS_LABEL = {
    "current": "现行", "new": "本批新增", "updated": "本批升版", "changed": "有变更单",
    "conflict": "冲突", "pending_void": "作废还是漏发待确认", "voided": "已作废", "missing": "缺失",
}
CONF_LABEL = {"confirmed": "人工确认", "high": "高置信", "mid": "中置信", "low": "低置信", "conflict": "冲突"}

DISCIPLINES = [("建", "建筑"), ("结", "结构"), ("水", "给排水"), ("暖", "暖通"), ("电", "电气"),
               ("A", "建筑"), ("S", "结构"), ("P", "给排水"), ("M", "暖通"), ("N", "暖通"), ("E", "电气"), ("D", "电气")]
FACT_GROUPS = [
    ("工程概况", ("层数", "建筑高度", "屋面标高", "结构形式", "抗震", "面积", "耐火", "防水等级")),
    ("楼层", ("层高",)),
    ("结构构件", ("截面", "墙厚", "混凝土", "钢筋")),
]


# ---------------- 规范化 ----------------

def norm_no(s: str | None) -> str | None:
    """图号规范化：去空格，统一连接符，数字补到两位。建施05 / 建施 -5 / 建施－05 → 建施-05。"""
    if not s:
        return None
    s = re.sub(r"\s+", "", s).replace("－", "-").replace("—", "-").replace("_", "-").upper()
    m = re.match(r"^(.*?)-?(\d+)([A-Z]?)$", s)
    if not m or not m.group(1):
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}{m.group(3)}"


def no_parts(no: str) -> tuple[str, int | None]:
    m = re.match(r"^(.*)-(\d+)[A-Z]?$", no)
    return (m.group(1), int(m.group(2))) if m else (no, None)


def discipline_of(no: str) -> str:
    for prefix, name in DISCIPLINES:
        if no.startswith(prefix):
            return name
    return "其他"


def rev_key(rev: str | None) -> tuple | None:
    """版次排序键：A<B<C，0<1<2；无法比较返回 None。"""
    if not rev:
        return None
    r = re.sub(r"[版第次\s]", "", rev).upper()
    m = re.match(r"^([A-Z]*)(\d*)$", r)
    if not m or not r:
        return None
    letters = sum((ord(c) - 64) * 27 ** i for i, c in enumerate(reversed(m.group(1))))
    return (letters, int(m.group(2) or 0))


def norm_value(v) -> str:
    s = re.sub(r"\s+", "", str(v)).replace("－", "-").replace("—", "-")
    try:
        return format(float(s), "g")
    except ValueError:
        pass
    s = s.replace("-", "")
    return s[:-2] if s.endswith("结构") else s


def norm_fact_key(name: str) -> str:
    return re.sub(r"\s+", "", name).replace("～", "~")


def fact_group(name: str, is_standard: bool) -> str:
    if is_standard:
        return "执行规范"
    for g, words in FACT_GROUPS:
        if any(w in name for w in words):
            return g
    return "其他"


# ---------------- 数据装载 ----------------

@dataclass
class PageRec:
    page_id: str
    file_sha: str
    page_index: int
    render_hash: str
    dhash: str
    text: str
    paths: list            # 截至当前批次的所有出处
    batches: list          # 出现过的批次号
    first_batch: int
    rec: dict | None       # 识别结果（自己的或复用像素相同页面的）
    rec_borrowed: bool = False


@dataclass
class Sheet:
    no: str
    title: str | None = None
    discipline: str = "其他"
    pages: list = field(default_factory=list)  # [(rev, PageRec)]


def _load(con, as_of: int):
    pages_rows = con.execute(
        "SELECT p.* FROM pages p WHERE EXISTS (SELECT 1 FROM occurrences o WHERE o.sha256=p.file_sha256 AND o.batch_seq<=?)",
        (as_of,),
    ).fetchall()
    occ = defaultdict(list)
    for o in con.execute("SELECT * FROM occurrences WHERE batch_seq<=? ORDER BY batch_seq, id", (as_of,)):
        occ[o["sha256"]].append(o)
    recs = {r["page_id"]: json.loads(r["data"]) for r in con.execute("SELECT * FROM recognitions")}
    by_hash: dict[str, dict] = {}
    for r in pages_rows:
        rec = recs.get(r["page_id"])
        if rec is None:
            continue
        old = by_hash.get(r["render_hash"])
        if old is None or (old["run"]["executor"] in PLACEHOLDER_EXECUTORS
                           and rec["run"]["executor"] not in PLACEHOLDER_EXECUTORS):
            by_hash[r["render_hash"]] = rec
    out = []
    for r in pages_rows:
        o = occ[r["file_sha256"]]
        rec = recs.get(r["page_id"])
        borrowed = False
        best = by_hash.get(r["render_hash"])
        # 像素相同的页面共用识别结果；有真实执行器的结果时，不用模拟结果
        if best is not None and (rec is None or (rec["run"]["executor"] in PLACEHOLDER_EXECUTORS
                                                 and best["run"]["executor"] not in PLACEHOLDER_EXECUTORS)):
            rec, borrowed = best, best is not recs.get(r["page_id"])
        out.append(PageRec(
            page_id=r["page_id"], file_sha=r["file_sha256"], page_index=r["page_index"],
            render_hash=r["render_hash"], dhash=r["dhash"], text=r["text"],
            paths=[x["path"] + (f"（第 {r['page_index']} 页）" if _multi_page(con, r["file_sha256"]) else "") for x in o],
            batches=sorted({x["batch_seq"] for x in o}), first_batch=min(x["batch_seq"] for x in o),
            rec=rec, rec_borrowed=borrowed,
        ))
    return out


_mp_cache: dict = {}


def _multi_page(con, sha: str) -> bool:
    if sha not in _mp_cache:
        _mp_cache[sha] = (con.execute("SELECT page_count FROM files WHERE sha256=?", (sha,)).fetchone() or [1])[0] > 1
    return _mp_cache[sha]


def _is_archive_name(name: str) -> bool:
    return name.lower().endswith((".zip", ".rar", ".7z", ".tar", ".tgz", ".gz"))


def _in_nested_archive(path: str) -> bool:
    """出处路径形如“批次#3 / 外层.zip / 结构 / 补充图纸.zip / 结施-03.pdf”，中间再出现压缩包即为内层。

    文件夹批次形如“批次#5 / 补充（文件夹） / 结构 / 结构.zip / 结施-06.pdf”：文件夹里直接放的压缩包相当于外层，
    压缩包里再套的压缩包才算内层。
    """
    parts = path.split(" / ")
    if len(parts) > 1 and parts[1].endswith(FOLDER_MARK):
        return sum(_is_archive_name(p) for p in parts[2:-1]) >= 2
    return any(_is_archive_name(p) for p in parts[2:-1])


def _val(rec, key):
    return (rec["title_block"].get(key) or {}).get("value")


def _same_content(a: PageRec, b: PageRec) -> bool:
    if a.render_hash == b.render_hash:
        return True
    return (pdfpages.text_similarity(a.text, b.text) >= SAME_TEXT
            and pdfpages.dhash_distance(a.dhash, b.dhash) <= SAME_DHASH)


# ---------------- 主计算 ----------------

def _assistant_rows(con) -> dict:
    """AI 助手的待确认提议和已确认的项目笔记。它们是原始输入，不参与派生计算，只随状态一起给界面看。"""
    props = []
    for r in con.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY id"):
        props.append({"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"]), "reason": r["reason"],
                      "source": r["source"], "created_at": r["created_at"]})
    notes = [dict(r) for r in con.execute("SELECT * FROM notes ORDER BY id")]
    return {"proposals": props, "notes": notes}


def build(proj: Project, as_of: int | None = None, with_diff: bool = True) -> dict:
    with proj.connect() as con:
        batches = [dict(b) for b in con.execute("SELECT * FROM batches ORDER BY seq")]
        assistant = _assistant_rows(con)
        if not batches:
            return {"project": proj.meta, "generated_at": now_iso(), "as_of": 0, "batches": [], "sheets": [],
                    "edges": [], "issues": [], "facts": {"groups": []}, "baselines": [], "notices": [],
                    "floor_coverage": [], "recognition": {"pages": 0, "recognized": 0, "executors": {}},
                    "security": {"files": [], "scans": [], "pending": 0}, "classification": None, **assistant}
        labels = classify.load_labels(con)
        as_of = as_of or batches[-1]["seq"]
        batches = [b for b in batches if b["seq"] <= as_of]
        decisions = latest_decisions(con)
        pages = _load(con, as_of)
        ext_issues = [dict(r) for r in con.execute(
            "SELECT * FROM extraction_issues WHERE batch_seq<=? ORDER BY id", (as_of,))]
        baselines_rows = [dict(r) for r in con.execute("SELECT * FROM baselines WHERE as_of_batch<=?", (as_of,))]
        file_counts = {r["batch_seq"]: dict(r) for r in con.execute(
            "SELECT o.batch_seq, COUNT(*) AS n, SUM(f.kind='pdf') AS pdf, SUM(o.depth>1) AS nested"
            " FROM occurrences o JOIN files f ON f.sha256=o.sha256 WHERE f.kind!='archive' GROUP BY o.batch_seq")}
        risk_rows = [dict(r) for r in con.execute("SELECT * FROM risk_findings WHERE batch_seq<=? ORDER BY id", (as_of,))]
        risk_rows += risk.blocked_rows(ext_issues)  # 收图时被杀毒软件拦下的文件，也按高风险文件处理
        scans = [dict(r) for r in con.execute("SELECT * FROM av_scans WHERE batch_seq<=? ORDER BY id", (as_of,))]
        risk_paths: dict[str, list] = defaultdict(list)
        for r in con.execute("SELECT o.sha256, o.path FROM occurrences o WHERE o.batch_seq<=? AND o.sha256 IN"
                             " (SELECT sha256 FROM risk_findings) ORDER BY o.batch_seq, o.id", (as_of,)):
            risk_paths[r["sha256"]].append(r["path"])

    state = _compute(proj, batches, pages, decisions, ext_issues, baselines_rows, file_counts, as_of, labels)
    state["classification"] = classify.summary(labels, state)
    state["security"] = _security(risk_rows, scans, risk_paths, decisions, state["issues"])
    state["issues"].sort(key=lambda i: (ISSUE_ORDER[i["level"]], i["kind"], i["title"]))
    state.update(assistant)
    if with_diff and as_of > 1:
        prev = build(proj, as_of - 1, with_diff=False)
        state["diff"] = diff_states(prev, state)
    elif with_diff:
        state["diff"] = diff_states(None, state)
    return state


def _compute(proj, batches, pages, decisions, ext_issues, baselines_rows, file_counts, as_of, labels=None):
    labels = labels or {"sheet": {}, "fact": {}, "last": None}

    def disc(no: str) -> str:  # 按图号认不出专业的，用智能分类给的
        return classify.sheet_discipline(labels["sheet"].get(no), discipline_of(no))

    sheets: dict[str, Sheet] = {}
    notices = []  # 变更单、答疑等非图纸文件
    unrecognized = []
    for p in pages:
        if p.rec is None:
            unrecognized.append(p)
            continue
        kind = p.rec["page_kind"]
        no = norm_no(_val(p.rec, "drawing_no"))
        if kind in ("变更单", "答疑或澄清"):
            notices.append(p)
            continue
        if not no:
            continue
        s = sheets.setdefault(no, Sheet(no=no, discipline=disc(no)))
        s.pages.append((_val(p.rec, "revision"), p))

    issues: list[dict] = []
    current_page: dict[str, PageRec] = {}
    sheet_out: dict[str, dict] = {}

    # ---- 版本链与现行版 ----
    for no, s in sorted(sheets.items()):
        by_rev: dict[str, list[PageRec]] = defaultdict(list)
        for rev, p in s.pages:
            by_rev[rev or "?"].append(p)
        revisions = []
        for rev, plist in by_rev.items():
            groups: list[list[PageRec]] = []
            for p in sorted(plist, key=lambda x: x.first_batch):
                for g in groups:
                    if _same_content(g[0], p):
                        g.append(p)
                        break
                else:
                    groups.append([p])
            first_batch = min(p.first_batch for p in plist)
            date = next((_val(p.rec, "issue_date") for p in plist if _val(p.rec, "issue_date")), None)
            revisions.append({"rev": rev, "key": rev_key(rev), "date": date, "first_batch": first_batch,
                              "groups": groups})
        revisions.sort(key=lambda r: (r["key"] is None, r["key"] or (0, 0), r["date"] or "", r["first_batch"]))
        override = decisions.get(("sheet_current", no))
        cur = revisions[-1]
        if override:
            cur = next((r for r in revisions if r["rev"] == override["value"]), cur)
        status, notes = "current", []

        # 同图号同版次但内容不同
        pick = decisions.get(("sheet_pick", f"{no}|{cur['rev']}"))
        conflict_groups = len(cur["groups"]) > 1
        chosen_group = cur["groups"][-1]
        if conflict_groups and pick:
            chosen_group = next((g for g in cur["groups"] if any(p.page_id == pick["value"] for p in g)), chosen_group)
            conflict_groups = False
            notes.append(f"{cur['rev']} 版存在内容不同的多个文件，已按人工决定采用其中一份。")
        cp = chosen_group[0]
        current_page[no] = cp
        title = _val(cp.rec, "drawing_title")
        s.title = title
        if conflict_groups:
            status = "conflict"
            batches_desc = "、".join(f"批次#{g[0].first_batch}" for g in cur["groups"])
            issues.append(_issue("crit", "版本冲突", f"rev_dup:{no}", f"{no} {title or ''}".strip(),
                                 f"{batches_desc} 的 {no} 都标为 {cur['rev']} 版，但内容不同，可能改图未升版。",
                                 {"sheet": no},
                                 [{"label": f"采用批次#{g[0].first_batch} 的文件", "kind": "sheet_pick",
                                   "key": f"{no}|{cur['rev']}", "value": g[0].page_id} for g in cur["groups"]]))
        # 版次倒退：更晚的批次带来更低的版次
        for r in revisions:
            if r is not cur and r["key"] and cur["key"] and r["key"] < cur["key"] and r["first_batch"] > cur["first_batch"]:
                status = "conflict"
                issues.append(_issue("crit", "版本冲突", f"rev_regress:{no}", f"{no} 版次倒退",
                                     f"批次#{r['first_batch']} 带来 {r['rev']} 版，比批次#{cur['first_batch']} 的 {cur['rev']} 版更低。",
                                     {"sheet": no},
                                     [{"label": f"以 {x['rev']} 版为现行", "kind": "sheet_current", "key": no, "value": x["rev"]}
                                      for x in (cur, r)]))
        if status == "current":
            if len(revisions) > 1 and cur["first_batch"] == as_of:
                status = "updated"
            elif cur["first_batch"] == as_of and as_of > 1 and min(r["first_batch"] for r in revisions) == as_of:
                status = "new"
        vlist = []
        for r in revisions:
            for g in r["groups"]:
                state_txt = "现行" if (r is cur and g is chosen_group) else ("内容不同，待确认" if r is cur and conflict_groups else "已替代")
                vlist.append({"rev": r["rev"], "date": r["date"], "batch": g[0].first_batch, "state": state_txt,
                              "page_id": g[0].page_id, "paths": sorted({x for p in g for x in p.paths})})
        sheet_out[no] = {
            "no": no, "title": title, "discipline": s.discipline, "status": status,
            "current_rev": cur["rev"], "current_page": cp.page_id, "versions": vlist,
            "paths": cp.paths, "inner": all(_in_nested_archive(x) for x in cp.paths),
            "notes": notes, "borrowed": cp.rec_borrowed, "own_values": [],
            "rec_notes": [{"type": x["type"], "text": x["description"]} for x in cp.rec["issues"]],
            "cls": classify.sheet_view(labels["sheet"].get(no), discipline_of(no)),
        }

    # ---- 变更单 ----
    notice_out = []
    for p in notices:
        affected = sorted({norm_no(r["target_drawing_no"]) for r in p.rec["references"] if r["target_drawing_no"]})
        nid = re.sub(r"\s+", "", _val(p.rec, "drawing_no") or "") or p.page_id
        notice_out.append({"id": nid, "title": _val(p.rec, "drawing_title"), "kind": p.rec["page_kind"],
                           "page_id": p.page_id, "batch": p.first_batch, "affects": affected, "paths": p.paths})
        for no in affected:
            if no in sheet_out:
                sheet_out[no]["notes"].append(f"{nid}（批次#{p.first_batch}）修改了此图的局部，相关内容以变更文件为准。")
                if sheet_out[no]["status"] in ("current", "updated", "new"):
                    sheet_out[no]["status"] = "changed"

    # ---- 批次性质 ----
    known_before: set[str] = set()
    batch_out = []
    sheets_in_batch = defaultdict(set)
    for no, s in sheets.items():
        for _, p in s.pages:
            for b in p.batches:
                sheets_in_batch[b].add(no)
    catalog_in_batch = defaultdict(bool)
    for s in sheets.values():
        for _, p in s.pages:
            if p.rec["page_kind"] == "图纸目录":
                for b in p.batches:
                    catalog_in_batch[b] = True
    notice_batches = {n["batch"] for n in notice_out}
    pending_void: dict[str, int] = {}
    for b in batches:
        seq = b["seq"]
        here = sheets_in_batch[seq]
        kinds, conf = [], "中"
        if seq == 1 or not known_before:
            kinds, conf = ["首次发图"], "高"
        else:
            cover = len(here & known_before) / max(len(known_before), 1)
            name = b["name"] + b["archive_name"]
            if cover >= FULL_REISSUE or (catalog_in_batch[seq] and cover >= 0.5):
                kinds, conf = ["整套重发"], "高" if cover >= FULL_REISSUE else "中"
                for no in sorted(known_before - here):
                    pending_void[no] = seq
            elif here & known_before:
                kinds.append("部分修改")
            if here - known_before and "整套重发" not in kinds:
                kinds.append("补充图纸")
            if seq in notice_batches:
                kinds.append("变更单")
            if re.search(r"作废", name):
                kinds.append("作废通知")
            if not kinds:
                kinds, conf = ["无图纸"], "低"
        for no in here:
            pending_void.pop(no, None)  # 后来又出现了
        known_before |= here
        fc = file_counts.get(seq, {})
        batch_out.append({
            "seq": seq, "name": b["name"], "received_at": b["received_at"], "archive": b["archive_name"],
            "kinds": kinds, "confidence": conf, "sheets": len(here),
            "files": fc.get("n", 0), "pdf": fc.get("pdf", 0) or 0,
            "issues": [i for i in ext_issues if i["batch_seq"] == seq],
        })

    for no, seq in pending_void.items():
        d = decisions.get(("sheet_void", no))
        if d and d["value"] == "voided":
            sheet_out[no]["status"] = "voided"
            continue
        if d and d["value"] == "keep":
            continue
        sheet_out[no]["status"] = "pending_void"
        issues.append(_issue("warn", "作废还是漏发", f"void:{no}", f"{no} {sheet_out[no]['title'] or ''}".strip(),
                             f"批次#{seq} 判断为整套重发，但其中没有这张图。是作废了，还是漏发了？",
                             {"sheet": no},
                             [{"label": "确认已作废", "kind": "sheet_void", "key": no, "value": "voided"},
                              {"label": "仍然有效（漏发）", "kind": "sheet_void", "key": no, "value": "keep"}]))

    live = {no for no, s in sheet_out.items() if s["status"] != "voided"}

    # ---- 每张图管哪几层（图纸卡片上的同层图纸、楼层覆盖都用） ----
    for no, s in sheet_out.items():
        _set_floors(s, *_sheet_floors(s["title"], current_page[no]))

    # ---- 关系 ----
    edges, dangling = [], defaultdict(list)
    for no, p in current_page.items():
        if no not in live:
            continue
        for r in p.rec["references"]:
            t = norm_no(r["target_drawing_no"])
            if not t or t == no:
                continue
            etype = "cross" if disc(t) != disc(no) else "ref"
            edges.append({"from": no, "to": t, "type": etype, "label": r["raw_text"], "bbox": r["bbox"],
                          "page_id": p.page_id})
            if t not in live:
                dangling[t].append(no)

    # ---- 缺图 ----
    listed: dict[str, dict] = {}
    for no, p in current_page.items():
        if p.rec["page_kind"] == "图纸目录" and no in live:
            for e in p.rec["catalog_entries"]:
                t = norm_no(e["drawing_no"])
                if t:
                    listed[t] = {"catalog": no, "title": e["drawing_title"], "rev": e["revision"]}
    missing: dict[str, dict] = {}
    for t, info in listed.items():
        if t not in sheet_out:
            missing.setdefault(t, {"no": t, "title": info["title"], "evidence": [], "level": "高", "related": set()})
            missing[t]["evidence"].append(f"目录 {info['catalog']} 列出")
            missing[t]["related"].add(info["catalog"])
            edges.append({"from": info["catalog"], "to": t, "type": "catalog", "label": "目录列出", "bbox": None,
                          "page_id": current_page[info["catalog"]].page_id})
    for t, froms in dangling.items():
        if t in sheet_out:
            continue  # 已作废的图仍被引用：在下面单独提示
        missing.setdefault(t, {"no": t, "title": None, "evidence": [], "level": "高", "related": set()})
        missing[t]["evidence"].append("被 " + "、".join(sorted(set(froms))) + " 引用")
        missing[t]["related"] |= set(froms)
    for t, froms in dangling.items():
        if t in sheet_out and sheet_out[t]["status"] == "voided":
            issues.append(_issue("warn", "引用已作废图纸", f"ref_void:{t}", f"{t} 已作废但仍被引用",
                                 "引用它的图纸：" + "、".join(sorted(set(froms))), {"sheet": t}, [], sheets=froms))
    # 编号断号
    by_prefix = defaultdict(set)
    for no in list(sheet_out) + list(listed):
        pre, n = no_parts(no)
        if n is not None:
            by_prefix[pre].add(n)
    for pre, nums in by_prefix.items():
        if len(nums) < 3:
            continue
        ordered = sorted(nums)
        for a, b in zip(ordered, ordered[1:]):
            # 只在同一百位段里、连续缺几张时才算断号；更大的空档多半是分段编号（1xx 平面、2xx 剖面、9xx 系统图）
            if not (1 < b - a <= GAP_MAX + 1 and a // 100 == b // 100):
                continue
            for n in range(a + 1, b):
                t = f"{pre}-{n:02d}"
                if t not in missing:
                    missing[t] = {"no": t, "title": None, "evidence": [f"{pre} 编号断号"], "level": "中", "gap": True}
    # 还没识别的页面：文件名里就有这个图号的，多半只是还没识别到，先不算缺图（识别途中、只识别了前几档时常见）
    pending_names = "\n".join(sorted({x.split(" / ")[-1] for p in unrecognized for x in p.paths}))
    pending: list[tuple[str, str]] = []
    for t, m in sorted(missing.items()):
        d = decisions.get(("missing", t))
        if d and d["value"] == "not_needed":
            continue
        hit = _in_pending(t, pending_names)
        if hit:
            pending.append((t, hit))
            continue
        if d and d["value"] == "ask":
            m["confirmed"] = True
        m["status"] = "missing"
        desc = "；".join(m["evidence"]) + "，但截至目前所有批次中都没有。"
        issues.append(_issue("crit" if m["level"] == "高" else "warn", "缺图",
                             f"missing:{t}", f"{t} {m['title'] or ''}".strip(), desc, {"sheet": t},
                             [{"label": "列入答疑，向招标方要图", "kind": "missing", "key": t, "value": "ask"},
                              {"label": "确认不需要（作废或笔误）", "kind": "missing", "key": t, "value": "not_needed"}],
                             level_word=m["level"], confirmed=bool(m.get("confirmed")), sheets=m.get("related")))
    missing = {t: m for t, m in missing.items() if m.get("status") == "missing"}
    if pending:
        eg = "；".join(f"{t}（{name}）" for t, name in pending[:5]) + ("……" if len(pending) > 5 else "")
        issues.append(_issue("info", "待识别", "pending_refs", f"{len(pending)} 个被引用或目录列出的图号还没识别",
                             f"它们在还没识别的页面的文件名里：{eg}。识别完再判断是否缺图。", {}, []))

    # ---- 目录版次 ----
    _catalog_revisions(sheet_out, current_page, decisions, issues, live)

    # ---- 解包问题 ----
    for i in ext_issues:
        if i["kind"] == "blocked":
            continue  # 被杀毒软件拦下的文件在“风险文件”里处理
        label = {"encrypted": "加密", "corrupt": "损坏", "no_tool": "缺少解压工具", "volume": "分卷",
                 "too_deep": "嵌套过深", "too_large": "超出上限", "symlink": "符号链接",
                 "unreadable": "读不出来"}.get(i["kind"], i["kind"])
        if i["kind"] == "symlink":
            issues.append(_issue("info", "解包问题", f"extract:{i['id']}", f"{label}：{i['path'].split(' / ')[-1]}",
                                 f"{i['detail']}。位置：{i['path']}。", {"batch": i["batch_seq"]}, []))
            continue
        issues.append(_issue("warn", "解包问题", f"extract:{i['id']}", f"{label}：{i['path'].split(' / ')[-1]}",
                             f"{i['detail'].rstrip('。')}。位置：{i['path']}。这里面可能藏着缺失的图纸。", {"batch": i["batch_seq"]}, []))

    # ---- 未识别页面 ----
    if unrecognized:
        issues.append(_issue("info", "待识别", "unrecognized", f"{len(unrecognized)} 页尚未识别",
                             "点上方的「识别」，范围选“新页面”接着识别（命令行：bidbench recognize）。"
                             "没识别的页面里的图，暂时不会出现在图谱和缺图检查里。", {}, []))
    reasons: dict[str, int] = defaultdict(int)  # 只数缺图号、字迹不清这类；“没写单位”之类的备注显示在图纸卡片上
    for p in pages:
        r = p.rec and weak_reason(p.rec)
        if r:
            reasons[r] += 1
    if reasons:
        issues.append(_issue("info", "识别不完整", "weak_rec", f"{sum(reasons.values())} 页识别不完整",
                             "其中" + "、".join(f"{r} {n} 页" for r, n in sorted(reasons.items(), key=lambda x: -x[1]))
                             + "。扫描件或图签不全的页面，识别时选“识别不完整的页面”交给多模态执行器复核。", {}, []))

    # ---- 事实层 ----
    facts = _facts(sheets, sheet_out, current_page, notice_out, decisions, issues, live, labels["fact"])

    # ---- 楼层覆盖 ----
    floor_coverage = _floor_coverage(sheet_out, current_page, facts, decisions, issues, live)

    # ---- 图纸基线 ----
    baselines = []
    for b in baselines_rows:
        snap = json.loads(b["snapshot"])
        outdated = [{"sheet": no, "base_rev": rev, "current_rev": sheet_out[no]["current_rev"]}
                    for no, rev in sorted(snap.items()) if no in sheet_out and sheet_out[no]["current_rev"] != rev]
        voided = [no for no in snap if no in sheet_out and sheet_out[no]["status"] == "voided"]
        baselines.append({"name": b["name"], "created_at": b["created_at"], "as_of_batch": b["as_of_batch"],
                          "sheets": len(snap), "outdated": outdated, "voided": voided, "note": b["note"]})
        if outdated or voided:
            desc = "；".join(f"{o['sheet']} {o['base_rev']}→{o['current_rev']}" for o in outdated)
            if voided:
                desc += ("；" if desc else "") + "已作废：" + "、".join(voided)
            issues.append(_issue("warn", "基线过期", f"baseline:{b['name']}", f"任务「{b['name']}」用的图纸已更新",
                                 desc + "。", {"sheet": (outdated[0]["sheet"] if outdated else voided[0])}, [],
                                 sheets=[o["sheet"] for o in outdated] + voided))

    # ---- 汇总 ----
    sheets_list = list(sheet_out.values())
    for t, m in sorted(missing.items()):
        out = {"no": t, "title": m["title"], "discipline": disc(t), "status": "missing",
               "current_rev": None, "current_page": None, "versions": [], "paths": [],
               "inner": False, "notes": m["evidence"], "missing_level": m["level"],
               "confirmed": bool(m.get("confirmed")), "borrowed": False, "own_values": [], "rec_notes": [],
               "cls": classify.sheet_view(labels["sheet"].get(t), discipline_of(t))}
        fl = floors.parse_floors(m["title"] or "")
        _set_floors(out, fl, "图名" if fl else "")
        sheets_list.append(out)
    sheets_list.sort(key=lambda s: (_disc_order(s["discipline"]), no_parts(s["no"])[0], no_parts(s["no"])[1] or 0, s["no"]))
    issues.sort(key=lambda i: (ISSUE_ORDER[i["level"]], i["kind"], i["title"]))
    executors = defaultdict(int)
    for p in pages:
        if p.rec and not p.rec_borrowed:
            executors[p.rec["run"]["executor"]] += 1
    return {
        "project": proj.meta, "generated_at": now_iso(), "as_of": as_of,
        "batches": batch_out, "sheets": sheets_list, "edges": edges, "notices": notice_out,
        "issues": issues, "facts": facts, "baselines": baselines, "floor_coverage": floor_coverage,
        "recognition": {"pages": len(pages), "recognized": sum(1 for p in pages if p.rec),
                        "borrowed": sum(1 for p in pages if p.rec_borrowed), "executors": dict(executors)},
    }


def _in_pending(no: str, names: str) -> str | None:
    """图号出现在还没识别的页面的文件名里时，返回那个文件名。"""
    pre, n = no_parts(no)
    if not names or n is None:
        return None
    m = re.search(rf"(?<![A-Za-z0-9\u4e00-\u9fff]){re.escape(pre)}\s*[-－—_]?\s*0*{n}(?![0-9])", names, re.I)
    if not m:
        return None
    start = names.rfind("\n", 0, m.start()) + 1
    end = names.find("\n", m.end())
    return names[start:end if end >= 0 else None]


def _rev_scheme(rev: str | None) -> str | None:
    """版次的写法：字母（A、B）、数字（0、1、第2版）、字母加数字（A1）。写法不同的版次没法比新旧。"""
    r = re.sub(r"[版第次\s]", "", rev or "").upper()
    return "letter" if re.fullmatch(r"[A-Z]+", r) else "number" if re.fullmatch(r"\d+", r) else (
        "mixed" if re.fullmatch(r"[A-Z]+\d+", r) else None)


def _catalog_revisions(sheet_out, current_page, decisions, issues, live) -> None:
    """现行目录上写的版次，和收到的现行版次对账。

    目录写的版次更新：可能还缺新版图，手上的旧版不能放心用（严重，逐张提示）。
    目录写的版次更旧：目录没跟着图纸更新（提醒，每份目录合成一条）。
    """
    for cat, p in sorted(current_page.items()):
        if p.rec["page_kind"] != "图纸目录" or cat not in live:
            continue
        cat_rev = sheet_out[cat]["current_rev"]
        behind = []
        for e in p.rec["catalog_entries"]:
            t = norm_no(e["drawing_no"])
            if not t or t == cat or t not in live:
                continue  # 没收到的图在“缺图”里提示
            listed, cur = e["revision"], sheet_out[t]["current_rev"]
            if not _rev_scheme(listed) or _rev_scheme(listed) != _rev_scheme(cur) or rev_key(listed) == rev_key(cur):
                continue
            if rev_key(listed) < rev_key(cur):
                behind.append((t, listed, cur))
                continue
            key = f"{t}|{listed}"
            d = decisions.get(("catalog_rev", key))
            if d and d["value"] == "ignore":
                continue
            issue = _issue("crit", "目录版次", f"catalog_newer:{cat}:{t}", f"{t} 目录写 {listed} 版，手上只有 {cur} 版",
                           f"目录 {cat}（{cat_rev} 版）列出 {t} 为 {listed} 版，但截至目前收到的最新是 {cur} 版。"
                           f"可能还缺 {listed} 版图纸，手上的 {cur} 版可能已经过时。",
                           {"sheet": t},
                           [{"label": f"列入答疑，向招标方要 {listed} 版", "kind": "catalog_rev", "key": key, "value": "ask"},
                            {"label": f"目录笔误，以 {cur} 版为准", "kind": "catalog_rev", "key": key, "value": "ignore"}],
                           confirmed=bool(d and d["value"] == "ask"))
            issue["qa"] = (f"图纸目录 {cat}（{cat_rev} 版）列出 {t} 为 {listed} 版，但收到的是 {cur} 版，"
                           f"请补发 {listed} 版，或确认以 {cur} 版为准。")
            issues.append(issue)
        if not behind:
            continue
        key = f"{cat}|{cat_rev}|" + ",".join(f"{t}:{cur}" for t, _listed, cur in behind)  # 又有图升版时重新提示
        d = decisions.get(("catalog_rev", key))
        if d and d["value"] == "ignore":
            continue
        rows = "；".join(f"{t} 目录 {listed} 版、收到 {cur} 版" for t, listed, cur in behind)
        issue = _issue("warn", "目录版次", f"catalog_behind:{cat}", f"{cat} 目录上的版次没跟上图纸",
                       f"目录 {cat}（{cat_rev} 版）上有 {len(behind)} 张图的版次比收到的旧：{rows}。"
                       "以收到的最新版为准即可；需要以目录为合同依据时，请发图方更新目录。",
                       {"sheet": cat},
                       [{"label": "列入答疑，请发图方更新目录", "kind": "catalog_rev", "key": key, "value": "ask"},
                        {"label": "知道了，以收到的版本为准", "kind": "catalog_rev", "key": key, "value": "ignore"}],
                       confirmed=bool(d and d["value"] == "ask"))
        issue["qa"] = (f"图纸目录 {cat}（{cat_rev} 版）上 " + "、".join(t for t, *_ in behind)
                       + f" 的版次比收到的旧（{rows}），请确认以收到的最新版为准，并提供更新后的图纸目录。")
        issues.append(issue)


def _fact_int(facts: dict, key: str) -> tuple[int | None, str | None]:
    """从事实层取整数（地上层数、地下层数），返回 (值, 说明)。冲突时按当前展示的值，并在说明里写明。"""
    for g in facts["groups"]:
        for f in g["facts"]:
            if f["key"] == key:
                m = re.search(r"\d+", f["value"])
                if not m:
                    return None, None
                note = f"{f['name']} {m.group(0)} 层（{CONF_LABEL[f['conf']]}）"
                if f["conf"] == "conflict":
                    note += "，该事实存在冲突，确认后结果可能变化"
                return int(m.group(0)), note
    return None, None


def _sheet_floors(title: str, page) -> tuple[set[int], str]:
    """一张图覆盖的楼层：先看图名；图名没有（如“标准层平面图”）再看“适用楼层”类说明和关键数值。"""
    got = floors.parse_floors(title or "")
    if got:
        return got, "图名"
    rec = page.rec
    for n in rec["content"]["notes"]:
        if floors.STANDARD_HINT.search(n["text"]):
            got = floors.parse_floors(n["text"])
            if got:
                return got, "说明"
    for kv in rec["content"]["key_values"]:
        if re.search(r"适用(?:楼层|范围)|楼层范围", kv["name"]):
            got = floors.parse_floors(f"{kv['value'] or ''} {kv['raw_text'] or ''}")
            if got:
                return got, "说明"
    for kv in rec["content"]["key_values"]:
        if "层高" in kv["name"]:
            got |= floors.parse_floors(kv["name"].replace("层高", ""))
    return got, ("关键数值" if got else "")


def _set_floors(s: dict, fl: set[int], how: str) -> None:
    title = s["title"] or ""
    s["floors"], s["floors_from"], s["floors_text"] = sorted(fl), how, floors.compress(fl) or None
    # 通用图只看图名：没写楼层的柱平面布置图，即使层高表里列了各层，也是建哪一层都要看的图
    s["general"] = bool(GENERAL_SHEET.search(title)) and "目录" not in title and not floors.parse_floors(title)


def same_floor(st: dict, no: str, limit: int | None = None) -> dict:
    """一张图的同层图纸（各专业管同一层的图）和通用图（设计说明、没写楼层的柱平面等）。

    同层的按专业排，楼层完全一样的排前面；有严重问题的图带上问题标题，从这里就能看到。"""
    by_no = {s["no"]: s for s in st["sheets"]}
    s = by_no[no]
    crit = defaultdict(list)
    for i in st["issues"]:
        if i["level"] == "crit" and i["kind"] != "缺图" and i["target"].get("sheet"):  # 缺图看状态就知道
            crit[i["target"]["sheet"]].append(i["title"])

    def row(t):
        return {"no": t["no"], "title": t["title"], "discipline": t["discipline"], "status": STATUS_LABEL[t["status"]],
                "floors": t["floors_text"], "problems": crit.get(t["no"], [])}

    mine = set(s["floors"])
    near = [t for t in st["sheets"]
            if t["no"] != no and t["status"] != "voided" and not t["general"] and mine & set(t["floors"])]
    near.sort(key=lambda t: (_disc_order(t["discipline"]), len(mine ^ set(t["floors"])), no_parts(t["no"])[0],
                             no_parts(t["no"])[1] or 0))
    general = [t for t in st["sheets"] if t["no"] != no and t["status"] != "voided" and t["general"]]
    general.sort(key=lambda t: (_disc_order(t["discipline"]), no_parts(t["no"])[0], no_parts(t["no"])[1] or 0))
    out = {"floors": s["floors_text"], "same_floor": [row(t) for t in near[:limit]], "general": [row(t) for t in general]}
    if limit is not None and len(near) > limit:
        out["more_same_floor"] = len(near) - limit
    return out


def _floor_coverage(sheet_out, current_page, facts, decisions, issues, live) -> list[dict]:
    above, above_note = _fact_int(facts, "地上层数")
    below, below_note = _fact_int(facts, "地下层数")
    out = []
    for disc, gname, include, exclude, full in floors.GROUPS:
        members = []
        for no in sorted(live):
            s = sheet_out[no]
            title = s["title"] or ""
            if s["discipline"] != disc or not re.search(include, title) or (exclude and re.search(exclude, title)):
                continue
            members.append((no, title, set(s["floors"]), s["floors_from"]))
        if not members:
            continue
        covered: dict[int, list[str]] = defaultdict(list)
        for no, _title, fl, _how in members:
            for f in fl:
                covered[f].append(no)
        pending = {no for no, *_ in members if sheet_out[no]["status"] == "pending_void"}  # 作废还是漏发待确认
        firm = {f for no, _t, fl, _h in members if no not in pending for f in fl}
        tentative = sorted(set(covered) - firm)
        unknown = [m for m in members if not m[2]]
        for no, title, _fl, _how in unknown:
            if "标准层" in title:
                issues.append(_issue("warn", "楼层覆盖", f"floor_unknown:{no}", f"{no} {title} 未注明楼层范围",
                                     "图名和说明里都没有写适用楼层，无法判断标准层覆盖了哪几层。", {"sheet": no}, []))
        ground = sorted(f for f in covered if 0 < f < floors.ROOF)
        basis = None
        if full:
            top = above or (max(ground) if ground else None)
            expected = set(range(1, top + 1)) if top else set()
            expected |= {-n for n in range(1, (below or 0) + 1)}
            expected.add(floors.ROOF)
            basis = "；".join(x for x in (above_note, below_note) if x) or (
                f"事实层没有层数，按图纸里出现的最高楼层 {top}F 检查" if top else None)
        elif len(ground) >= 2:
            expected = set(range(min(ground), max(ground) + 1))
            basis = f"只检查已出现楼层 {floors.label(min(ground))}~{floors.label(max(ground))} 之间的断档"
        else:
            expected = set()
            basis = ("图名和说明里都没有楼层信息，无法检查" if not ground
                     else f"只知道 {floors.label(ground[0])} 一层，无法判断有没有断档")
        missing = sorted(expected - set(covered))
        unchecked = None
        if not full and expected and above:  # 只查了已出现楼层之间：说清楚楼里还有哪些层没查
            building = set(range(1, above + 1)) | {-n for n in range(1, (below or 0) + 1)} | {floors.ROOF}
            unchecked = floors.compress(sorted(building - expected - set(covered))) or None
        remarks = []
        if unchecked:
            remarks.append(f"没查：{unchecked}（这类图只检查已出现楼层之间有没有断档）")
        if tentative:
            src = sorted({no for f in tentative for no in covered[f] if no in pending})
            remarks.append(f"{floors.compress(tentative)} 只有待确认是否作废的 {'、'.join(src)}，确认作废后会变成缺图")
        row = {
            "group": gname, "discipline": disc, "basis": basis,
            "covered": [{"floors": floors.compress(fl), "sheets": [no], "from": how, "tentative": no in pending}
                        for no, _t, fl, how in members if fl],
            "covered_text": floors.compress(covered),
            "unknown": [no for no, *_ in unknown],
            "missing": [floors.label(f) for f in missing], "missing_text": floors.compress(missing),
            "status": "missing" if missing else ("ok" if full and expected else "no_gap" if expected else "unchecked"),
            "unchecked": unchecked, "tentative": floors.compress(tentative) or None, "remarks": remarks,
        }
        if missing:
            key = f"楼层|{gname}|{row['missing_text']}"
            d = decisions.get(("missing", key))
            if d and d["value"] == "not_needed":
                row["status"] = "accepted"
            else:
                row["confirmed"] = bool(d and d["value"] == "ask")
                if len(row["covered"]) > COVER_LIST_MAX:  # 几十上百张图：只写楼层范围
                    have = f"{row['covered_text']}（{len(row['covered'])} 张图）" + (
                        f"，其中 {row['tentative']} 只有待确认是否作废的图" if row["tentative"] else "")
                else:
                    have = "；".join(f"{c['floors']}（{c['sheets'][0]}{'，待确认是否作废' if c['tentative'] else ''}）"
                                    for c in row["covered"]) or "无"
                issues.append(_issue(
                    "warn", "楼层覆盖", f"floor:{gname}", f"{gname}缺 {row['missing_text']}",
                    f"{basis}。已有：{have}。缺：{row['missing_text']}。", {"floor_group": gname},
                    [{"label": "列入答疑，向招标方要图", "kind": "missing", "key": key, "value": "ask"},
                     {"label": "确认不需要（已有图纸覆盖或该层不存在）", "kind": "missing", "key": key, "value": "not_needed"}],
                    level_word="中", confirmed=row["confirmed"]))
        out.append(row)
    return out


ISSUE_ORDER = {"crit": 0, "warn": 1, "info": 2}
RISK_DECISION = {"safe": "已确认安全", "dangerous": "已确认有风险"}


def _security(rows, scans, paths, decisions, issues) -> dict:
    """风险文件：按内容（SHA-256）合并各项发现。高风险算严重问题，中风险算提醒，低风险只写进报告。"""
    by_sha: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: risk.LEVEL_ORDER[r["level"]]):
        f = by_sha.setdefault(r["sha256"], {"sha": r["sha256"], "key": r["sha256"][:12], "level": r["level"],
                                            "findings": [], "sources": [], "first_batch": r["batch_seq"],
                                            "blocked": r["kind"] == "av_blocked"})
        if risk.LEVEL_ORDER[r["level"]] < risk.LEVEL_ORDER[f["level"]]:
            f["level"] = r["level"]
        if r["detail"] not in f["findings"]:
            f["findings"].append(r["detail"])
        if r["source"] not in f["sources"]:
            f["sources"].append(r["source"])
        if r["sha256"] not in paths and r["path"] not in f.setdefault("_paths", []):
            f["_paths"].append(r["path"])
    files = []
    for sha, f in by_sha.items():
        f["paths"] = paths.get(sha) or f.pop("_paths", [])
        f.pop("_paths", None)
        f["name"] = f["paths"][0].split(" / ")[-1] if f["paths"] else "（未定位）"
        f["level_word"] = risk.LEVEL_WORD[f["level"]]
        d = decisions.get(("risk", f["key"]))
        f["decision"] = d["value"] if d else None
        files.append(f)
        if f["level"] == "low" or f["decision"] == "safe":
            continue
        where = "；".join(f["paths"][:3]) + (f" 等 {len(f['paths'])} 处" if len(f["paths"]) > 3 else "")
        if f["blocked"]:  # 文件已被杀毒软件拦下，项目里没有它
            title, tail = f"{f['name']}（被杀毒软件拦截）", "如果它是图纸，需要发图方重新发一份干净的文件。"
            labels = ("确认是误报（来源可信）", "列入答疑，请发图方核实并重发")
        else:
            title, tail = f"{f['name']}（{f['level_word']}风险）", "不要双击打开，也不要和图纸放在同一个文件夹里用 CAD 打开。"
            labels = ("确认安全（来源可信）", "确认有风险，列入答疑请发图方核实")
        issues.append(_issue(
            "crit" if f["level"] == "high" else "warn", "风险文件", f"risk:{f['key']}", title,
            "；".join(f["findings"]) + f"。位置：{where}。{tail}",
            {"file": f["key"]},
            [{"label": labels[0], "kind": "risk", "key": f["key"], "value": "safe"},
             {"label": labels[1], "kind": "risk", "key": f["key"], "value": "dangerous"}],
            level_word=f["level_word"], confirmed=f["decision"] == "dangerous"))
    files.sort(key=lambda f: (risk.LEVEL_ORDER[f["level"]], f["name"]))
    pending = sum(1 for f in files if f["level"] != "low" and not f["decision"])
    return {"files": files, "scans": scans, "pending": pending}


def _disc_order(d: str) -> int:
    order = classify.DISCIPLINES
    return order.index(d) if d in order else len(order)


def _issue(level, kind, iid, title, detail, target, options, level_word=None, confirmed=False, sheets=None):
    """sheets：牵涉到的其他图纸（事实冲突的各个来源、引用了缺图的图纸等），图纸卡片上作为“相关问题”显示。"""
    return {"id": iid, "level": level, "kind": kind, "title": title, "detail": detail, "target": target,
            "options": options, "level_word": level_word, "confirmed": confirmed, "sheets": sorted(set(sheets or ()))}


BASIS_NOTE = re.compile(r"[（(][^（）()]*(?:含|包括|计入|不计|除)[^（）()]*[）)]")  # 统计口径的说明，如“（含屋顶机房层）”


def _basis_quote(evs: list) -> str:
    """来源原文带统计口径说明时引出来：数不一样，可能只是口径不同。"""
    t = next((e["text"] for e in evs if e["text"] and BASIS_NOTE.search(e["text"])), None)
    return f"，原文“{t}”" if t else ""


def _facts(sheets, sheet_out, current_page, notices, decisions, issues, live, fact_labels=None):
    """项目事实，按大类分组；每条带 path（大类下面的小类）：智能分类过的按 AI 的，没分过的按说法粗分。"""
    fact_labels = fact_labels or {}
    ev_by_key: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    is_std: dict[str, bool] = {}

    def add(key, name, value, unit, raw, sheet, rev, page, source, conf, active, std=False):
        names.setdefault(key, name)
        is_std[key] = std
        ev_by_key[key].append({"sheet": sheet, "rev": rev, "page_id": page.page_id, "value": value, "unit": unit,
                               "text": raw, "source": source, "confidence": conf, "active": active})

    for no, s in sheets.items():
        if no not in live:
            continue
        cur = current_page[no]
        for rev, p in s.pages:
            active = p.render_hash == cur.render_hash or (rev == sheet_out[no]["current_rev"] and p is cur)
            for kv in p.rec["content"]["key_values"]:
                if kv["value"] is None:
                    continue
                if SHEET_SCOPED.search(norm_fact_key(kv["name"])):  # 本层标高、适用楼层：只记在这张图上，不跨图比
                    own = sheet_out[no]["own_values"]
                    item = {"name": kv["name"], "value": str(kv["value"]), "unit": kv["unit"], "text": kv["raw_text"]}
                    if active and item not in own:
                        own.append(item)
                    continue
                add(norm_fact_key(kv["name"]), kv["name"], str(kv["value"]), kv["unit"], kv["raw_text"],
                    no, rev, p, kv["source"], kv["confidence"], active)
            for st in p.rec["content"]["standards"]:
                if not st["code"]:
                    continue
                key = re.sub(r"[（(].*$", "", st["code"]).replace(" ", "")
                add(key, st["name"] or key, st["code"], None, st["code"] + (f" {st['name']}" if st["name"] else ""),
                    no, rev, p, st["source"], st["confidence"], active, std=True)

    groups: dict[str, list] = defaultdict(list)
    for key, evs in ev_by_key.items():
        # 同一页面重复出现的同值证据只算一次
        uniq, seen = [], set()
        for e in evs:
            k = (e["sheet"], e["rev"], e["page_id"], norm_value(e["value"]))
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        act = [e for e in uniq if e["active"]]
        stale = [e for e in uniq if not e["active"]]
        if not act:
            continue
        values = defaultdict(list)
        for e in act:
            values[norm_value(e["value"])].append(e)
        ranked = sorted(values.values(), key=lambda es: (-len({e["sheet"] for e in es}), es[0]["sheet"]))
        best = ranked[0]
        display = best[0]["value"] + (f" {best[0]['unit']}" if best[0]["unit"] and best[0]["unit"] not in ("层", "度") else (best[0]["unit"] or ""))
        note = None
        d = decisions.get(("fact", key))
        if d:
            conf = "confirmed"
            display = d["value"]
            note = f"人工确认（{d['created_at'][:10]}）" + (f"：{d['note']}" if d["note"] else "")
        elif len(values) > 1:
            conf = "conflict"
            parts = "；".join(f"{es[0]['value']}（{'、'.join(sorted({e['sheet'] for e in es}))}{_basis_quote(es)}）" for es in ranked)
            note = f"来源不一致：{parts}。"
            if any(_basis_quote(es) for es in ranked):
                note += "带统计口径说明的，可能只是口径不同（例如含不含机房层），不一定是图纸矛盾，核对原文后再定。"
            issues.append(_issue("crit", "事实冲突", f"fact:{key}", f"{names[key]}：" + " / ".join(es[0]["value"] for es in ranked),
                                 note, {"fact": key},
                                 [{"label": f"采用 {es[0]['value']}", "kind": "fact", "key": key,
                                   "value": es[0]["value"] + (f" {es[0]['unit']}" if es[0]["unit"] else "")} for es in ranked],
                                 sheets={e["sheet"] for e in act}))
        else:
            srcs = {e["sheet"] for e in best}
            if len(srcs) >= 2:
                conf = "high"
            elif all(e["source"] in ("text_layer", "both") for e in best):
                conf = "mid"
            else:
                conf = "low"
        changed = [e for e in stale if norm_value(e["value"]) not in values]
        if changed and conf != "confirmed":
            old = changed[0]
            up = f"来源已升版：{old['sheet']} {old['rev']} 版为 {old['value']}，现行版为 {best[0]['value']}。"
            note = (note + " " if note else "") + up
        # 出处图全都被变更单改过：数值可能已经不对了（变更单的内容不进事实层，要打开变更单核对）
        act_sheets = {e["sheet"] for e in act}
        by_notice = sorted({n["id"] for n in notices if act_sheets & set(n["affects"])})
        if by_notice and conf != "confirmed" and all(any(x in n["affects"] for n in notices) for x in act_sheets):
            note = (note + " " if note else "") + (
                f"出处 {'、'.join(sorted(act_sheets))} 有{'、'.join(by_notice)}，这个数值可能已被修改，以变更单为准。")
        else:
            by_notice = []
        place = classify.fact_place(fact_labels.get(classify.fact_template(names[key])), is_std[key])
        top, path = place or (fact_group(names[key], is_std[key]), None)
        groups[top].append({
            "key": key, "name": names[key], "value": display, "conf": conf, "note": note,
            "evidence": act + stale, "sources": sorted(act_sheets), "notices": by_notice,
            "path": path, "std": is_std[key], "ai": place is not None,
        })
    for fs in groups.values():  # 还没智能分类的：同一说法有两条以上的归成一个小类（KL3 截面、Q1 截面 → 截面）
        fam = {f["key"]: classify.fact_family(f["name"]) for f in fs if f["path"] is None}
        n = Counter(fam.values())
        for f in fs:
            if f["path"] is None:
                f["path"] = [fam[f["key"]]] if n[fam[f["key"]]] >= 2 else []
    return {"groups": [{"id": g, "label": g, "facts": sorted(groups[g], key=lambda f: f["name"])}
                       for g in classify.FACT_TOPS if groups.get(g)]}


# ---------------- 差异报告 ----------------

def diff_states(prev: dict | None, cur: dict) -> dict:
    ps = {s["no"]: s for s in (prev or {}).get("sheets", [])}
    cs = {s["no"]: s for s in cur["sheets"]}
    out = {"batch": cur["as_of"], "upgraded": [], "added": [], "resolved_missing": [], "new_missing": [],
           "new_conflicts": [], "new_fact_conflicts": [], "pending_void": [], "fact_changes": [], "baselines": [],
           "floor_gaps": []}
    for no, s in cs.items():
        p = ps.get(no)
        if s["status"] == "missing":
            if not p or p["status"] != "missing":
                out["new_missing"].append(no)
            continue
        if p is None or p["status"] == "missing":
            out["added"].append(no)
            if p and p["status"] == "missing":
                out["resolved_missing"].append(no)
        elif p["current_rev"] != s["current_rev"]:
            out["upgraded"].append(f"{no} {p['current_rev']}→{s['current_rev']}")
        if s["status"] == "conflict" and (not p or p["status"] != "conflict"):
            out["new_conflicts"].append(no)
        if s["status"] == "pending_void" and (not p or p["status"] != "pending_void"):
            out["pending_void"].append(no)
    pf = {f["key"]: f for g in (prev or {}).get("facts", {}).get("groups", []) for f in g["facts"]}
    for g in cur["facts"]["groups"]:
        for f in g["facts"]:
            old = pf.get(f["key"])
            moved = _source_changes(old, f) if old else ""
            if old and (old["value"] != f["value"] or old["conf"] != f["conf"] or moved):
                out["fact_changes"].append(f"{f['name']}：{old['value']}（{CONF_LABEL[old['conf']]}）→ {f['value']}（{CONF_LABEL[f['conf']]}）"
                                           + (f"。{moved}" if moved else ""))
            elif not old and prev is not None:
                out["fact_changes"].append(f"新增 {f['name']}：{f['value']}")
            if prev is not None and f["conf"] == "conflict" and (not old or old["conf"] != "conflict"):
                out["new_fact_conflicts"].append(f"{f['name']}：{_values_text(f)}" + (f"。{moved}" if moved else ""))
    prev_gaps = {r["group"]: r["missing_text"] for r in (prev or {}).get("floor_coverage", []) if r["status"] == "missing"}
    for r in cur.get("floor_coverage", []):
        if r["status"] == "missing" and prev_gaps.get(r["group"]) != r["missing_text"]:
            out["floor_gaps"].append(f"{r['group']}缺 {r['missing_text']}")
    for b in cur["baselines"]:
        if b["outdated"]:
            out["baselines"].append(f"{b['name']}：" + "、".join(f"{o['sheet']} {o['base_rev']}→{o['current_rev']}" for o in b["outdated"]))
    return out


def _values_text(f: dict) -> str:
    """现行来源各自写的值：“20（建施-01、建施-07） / 21（结施-01）”。"""
    by_value: dict[str, list] = defaultdict(list)
    for e in f["evidence"]:
        if e["active"]:
            by_value[norm_value(e["value"])].append(e)
    ranked = sorted(by_value.values(), key=lambda es: -len({e["sheet"] for e in es}))
    return " / ".join(f"{es[0]['value']}（{'、'.join(sorted({e['sheet'] for e in es}))}）" for es in ranked)


def _source_changes(old: dict, new: dict) -> str:
    """事实的来源里哪张图改了数：“结施-01 B 版改为 21（原 A 版为 20）”。"""
    before = {e["sheet"]: e for e in old["evidence"] if e["active"]}
    seen_values = {norm_value(e["value"]) for e in before.values()}
    out = []
    for e in new["evidence"]:
        if not e["active"]:
            continue
        b = before.get(e["sheet"])
        rev = f" {e['rev']} 版" if e["rev"] else ""
        if b and norm_value(b["value"]) != norm_value(e["value"]):
            out.append(f"{e['sheet']}{rev}改为 {e['value']}（原{' ' + b['rev'] + ' 版' if b['rev'] else ''}为 {b['value']}）")
        elif not b and norm_value(e["value"]) not in seen_values:
            out.append(f"{e['sheet']}{rev}写的是 {e['value']}")
    return "；".join(dict.fromkeys(out))


def create_baseline(proj: Project, name: str, sheets: list[str] | None = None, note: str | None = None) -> dict:
    """锁定一份图纸基线：记下当前每张图的现行版次。以后图纸升版时，系统会提示这个任务用的图过期了。"""
    st = build(proj, with_diff=False)
    pick = {norm_no(x) for x in sheets} if sheets else None
    snap = {s["no"]: s["current_rev"] for s in st["sheets"]
            if s["current_rev"] and s["status"] not in ("missing", "voided") and (pick is None or s["no"] in pick)}
    if pick:
        unknown = pick - set(snap)
        if unknown:
            raise ValueError("这些图号不在现行图纸中：" + "、".join(sorted(unknown)))
    with proj.connect() as con:
        con.execute("INSERT OR REPLACE INTO baselines(name, created_at, as_of_batch, snapshot, note) VALUES (?,?,?,?,?)",
                    (name, now_iso(), st["as_of"], json.dumps(snap, ensure_ascii=False), note))
    return snap
