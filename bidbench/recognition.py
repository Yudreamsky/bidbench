"""识别：生成任务包、导入结果。

执行器（外部 agent、内置 agent、模拟执行器）只看任务包、只写 results/，
系统负责校验和导入，所以执行器可以随时替换。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

import jsonschema

from . import dwg, names, pdfpages
from .store import PLACEHOLDER_EXECUTORS, Project, now_iso

log = logging.getLogger(__name__)

TITLEBLOCK_CLIP = (0.62, 0.70, 1.0, 1.0)  # 图签常见位置：右下角


def load_schema() -> dict:
    return json.loads(resources.files("bidbench").joinpath("spec/schema.json").read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(load_schema())


SCOPES = {
    "unrecognized": "尚未识别的页面",
    "new": "尚未识别的页面，以及只有模拟执行器结果的页面",
    "weak": "识别不完整的页面：模拟执行器结果、字迹不清或图签缺失等问题、缺图号、图签把握低于 0.6",
    "conflict": "涉及版本冲突、改图未升版、后收到旧版或事实冲突的页面",
    "all": "全部页面",
}


SERIOUS_ISSUES = ("字迹不清", "图签缺失", "疑似多张图", "文字层与图像不一致")
NO_NUMBER_KINDS = ("变更单", "答疑或澄清", "封面")  # 这几类页面本来就可以没有图号
HIGH_SCORE, LOW_SCORE = 0.85, 0.6  # 识别给的 0–1 分数：0.85 及以上把握高，0.6 以下把握低（docs/图纸字段.md 第 3 节）


def weak_reason(rec: dict) -> str | None:
    """识别结果哪里不够用、需要复核；够用返回 None。

    “其他”类问题只是备注（如图签没有专业栏、没写单位），不算识别不完整。"""
    serious = [x["type"] for x in rec["issues"] if x["type"] in SERIOUS_ISSUES]
    if serious:
        return serious[0]
    tb = rec["title_block"]
    if not tb["drawing_no"]["value"] and rec["page_kind"] not in NO_NUMBER_KINDS:
        return "缺图号"
    if any((tb[k].get("confidence") or 0) < LOW_SCORE for k in ("drawing_no", "drawing_title", "revision") if tb[k]["value"]):
        return "图签把握低"
    return None


def _is_weak(rec: dict) -> bool:
    return rec["run"]["executor"] in PLACEHOLDER_EXECUTORS or weak_reason(rec) is not None


# ---- 识别先后：先识别对后面判断最有用的图（目录、说明、平面），详图、大样这些边角放最后 ----
PRIORITIES = [
    ("catalog", "图纸目录、清单"),
    ("notes", "设计说明"),
    ("plans", "平面、布置、配筋"),
    ("sections", "立面、剖面"),
    ("other", "其他图纸"),
    ("details", "详图、大样、表格"),
]
PRIORITY_LABEL = dict(PRIORITIES)
PRIORITY_RANK = {k: i for i, (k, _) in enumerate(PRIORITIES)}
# 按顺序匹配：先认目录、说明，再认详图（“楼梯平面详图”算详图），最后认剖面、平面
_PRIORITY_RULES = [
    ("catalog", re.compile(r"目录|图纸清单|封面|drawing (?:list|register|index|schedule)|document register|"
                           r"issue sheet|transmittal|cover sheet", re.I)),
    ("notes", re.compile(r"说明|图例|general notes?|\bnotes?\b|\bspecifications?\b|\bspec\b|\blegends?\b|\babbreviations?\b", re.I)),
    ("details", re.compile(r"详图|大样|节点|表(?!面)|\bdetails?\b|\bdetailing\b|\bschedules?\b|\btables?\b", re.I)),
    ("sections", re.compile(r"立面|剖面|\belevations?\b|\bsections?\b", re.I)),
    ("plans", re.compile(r"平面|布置|配筋|\bplans?\b|\bga\b|general arrangement|\blayouts?\b|setting out|"
                         r"\bframing\b|\bfoundations?\b", re.I)),
]


def priority_of(path: str | None) -> str:
    """一页图排第几档：先看文件名，认不出再看所在的文件夹（从里往外）；都认不出算“其他图纸”。"""
    for seg in reversed([x for x in re.split(r"\s+/\s+", path or "") if x]):
        if seg.startswith("批次#"):
            continue
        name = re.sub(r"\.(?:pdf|dwg|dxf|png|jpe?g|tiff?|zip|rar|7z)$", "", seg, flags=re.I)
        key = _kind_of(name, _PRIORITY_RULES)
        if key:
            return key
    return "other"


def _kind_of(name: str | None, rules) -> str | None:
    name = re.sub(r"[_\-.]+", " ", name or "")  # Level_01-GA_Plan 这类文件名按词切开
    for key, rx in rules:
        if rx.search(name):
            return key
    return None


def priority_plan(rows) -> list[dict]:
    """按档统计要识别的页面：每档几页，举几个文件名做例子。"""
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(priority_of(r["first_path"]), []).append(r)
    out = []
    for key, label in PRIORITIES:
        rs = groups.get(key)
        if rs:
            names = list(dict.fromkeys(r["first_path"].split(" / ")[-1] for r in rs))
            out.append({"key": key, "label": label, "pages": len(rs), "examples": names[:3]})
    return out


# ---- 分步识别（渐进式）----
# 第一步“地图”不看图：文件名、文件夹、PDF 自带的文字、DWG（收图时就读了），只在本机，不花钱。
# 后面几步才看图识别，按用处一层层往下：重点图（目录、清单、表格、说明的头两页）读全，先把全局搭起来；
# 再是建模要照着画的平面、立面、剖面；详图、说明正文、认不出种类的页按需（用到哪张识别哪张）。
# 每页归哪一步，先看第一步认出的图号能不能对上目录、DWG 里读到的图名（地图带路），再看文件名、文件夹。
# 识别过的页不再识别，所以每一步都可以随时停、下次接着来。
STAGES = [
    ("key", "重点图", "图纸目录、变更单答疑、各种清单和表，说明的头两页：读全，先把全局搭起来"),
    ("main", "建模主图", "平面、立面、剖面：建模要照着画的图"),
    ("deep", "按需深入", "详图大样、说明正文、认不出种类的页、DWG 已经读出内容的图：用到哪张再识别哪张"),
]
STAGE_LABEL = {k: v for k, v, _ in STAGES}
STAGE_RANK = {k: i for i, (k, _, _) in enumerate(STAGES)}
STAGE_GROUPS = [  # (分组, 属于哪一步, 名称)，同一步里按这个先后
    ("target", "key", "你指定要看图识别的图"),
    ("catalog", "key", "图纸目录、清单"),
    ("changes", "key", "变更单、答疑"),
    ("lists", "key", "表格、一览表"),
    ("notes_head", "key", "说明的头两页"),
    ("plans", "main", "平面、布置、配筋"),
    ("sections", "main", "立面、剖面"),
    ("details", "deep", "详图、大样"),
    ("notes_body", "deep", "说明正文"),
    ("dwg", "deep", "DWG 已读出内容的图"),
    ("other", "deep", "认不出种类的页"),
]
SKIP = "skip"  # 你指定只要地图、不用看图识别的图（信息深度 1、2），哪一步都不放
GROUP_STAGE = {g: st for g, st, _ in STAGE_GROUPS}
GROUP_LABEL = {g: lb for g, _, lb in STAGE_GROUPS}
GROUP_RANK = {g: i for i, (g, _, _) in enumerate(STAGE_GROUPS)}
NOTES_HEAD = 2  # 说明、规范书这类多页文件，头两页（封面、目录）算重点图，其余是正文
_LISTS = re.compile(r"一览|统计|清单|明细|表(?!面|示)|\bschedules?\b|\blists?\b|\bregisters?\b|\bsch\b|\btables?\b", re.I)
_CHANGES = re.compile(r"变更|修改通知|答疑|澄清|补遗|\baddend(?:um|a)\b|\bvariations?\b|change (?:order|notice)|\bRFI\b|"
                      r"\bbulletins?\b|\bclarifications?\b|\btender quer(?:y|ies)\b", re.I)
_RULE = dict(_PRIORITY_RULES)
_STAGE_RULES = [("catalog", _RULE["catalog"]), ("changes", _CHANGES), ("lists", _LISTS), ("notes", _RULE["notes"]), ("details", _RULE["details"]),
                ("sections", _RULE["sections"]), ("plans", _RULE["plans"])]


def map_hints(con) -> dict:
    """地图给的线索：已经知道的图号 → 图名（DWG 读出的图签、已识别的图签和目录条目），以及 DWG 已读出的图号。"""
    from .state import norm_no
    titles: dict[str, str] = {}
    dwg_nos: set[str] = set()
    for r in con.execute("SELECT executor, data FROM recognitions"):
        rec = json.loads(r["data"])
        tb = rec.get("title_block") or {}
        no = norm_no((tb.get("drawing_no") or {}).get("value"))
        title = (tb.get("drawing_title") or {}).get("value")
        if no and title:
            titles.setdefault(no, title)
        if no and r["executor"] == "dwg":
            dwg_nos.add(no)
        for e in rec.get("catalog_entries") or []:
            n = norm_no(e.get("drawing_no"))
            if n and e.get("drawing_title"):
                titles[n] = e["drawing_title"]  # 目录上的图名优先
    return {"titles": titles, "dwg": dwg_nos}


def depth_targets(proj: Project) -> dict:
    """你在图纸上指定的信息深度（人工决定 depth）落到页：want 是要看图识别、还没到那么深的页，skip 是指定不用看图识别的页。"""
    from .state import build
    from .store import latest_decisions
    with proj.connect() as con:
        if not any(k == "depth" for k, _ in latest_decisions(con)):
            return {"want": set(), "skip": set()}
    want, skip = set(), set()
    for sh in build(proj, with_diff=False)["sheets"]:
        t = sh.get("depth_target")
        if t is None:
            continue
        if t >= 3 and sh["depth"] < t:
            want |= set(sh["depth_pages"])
        elif t <= 2:
            skip |= set(sh["depth_pages"])
    return {"want": want, "skip": skip}


def page_path(r) -> str:
    """页面的出处：多页文件带“（第 n 页）”，和一级识别、图谱里的写法一样。"""
    return r["first_path"] + (f"（第 {r['page_index']} 页）" if (r["page_count"] or 1) > 1 else "")


def first_guess(r) -> dict | None:
    """第一步（地图）对这一页的推测：只看文件名、文件夹和 PDF 文字（names.guess）。"""
    return names.guess([page_path(r)], r["text"] or "")


def stage_group(r, hints: dict | None = None, guess: dict | None = None) -> str:
    """一页归哪一组（见 STAGE_GROUPS）：先看地图上这页的图名（目录、DWG 里读到的，或文件名里的），再看文件夹。"""
    from .state import norm_no
    hints = hints or {"titles": {}, "dwg": set()}
    if r["page_id"] in hints.get("skip", ()):
        return SKIP
    if r["page_id"] in hints.get("want", ()):
        return "target"
    g = guess if guess is not None else first_guess(r)
    no = norm_no(g["no"]["value"]) if g else None
    multi = (r["page_count"] or 1) > 1
    titles = [hints["titles"].get(no) if no else None, (g.get("title") or {}).get("value") if g else None]
    kind = next((k for k in (_kind_of(t, _STAGE_RULES) for t in titles if t) if k), None)
    if kind is None:
        segs = [x for x in re.split(r"\s+/\s+", r["first_path"] or "") if x and not x.startswith("批次#")]
        for seg in reversed(segs):
            kind = _kind_of(re.sub(r"\.(?:pdf|dwg|dxf|png|jpe?g|tiff?|zip|rar|7z)$", "", seg, flags=re.I), _STAGE_RULES)
            if kind:
                break
    if kind == "notes":
        return "notes_head" if not multi or r["page_index"] <= NOTES_HEAD else "notes_body"
    if kind in ("plans", "sections") and no in hints["dwg"]:
        return "dwg"  # 同图号的 DWG 已经读出图签、文字和引用，PDF 这页用到时再看
    if kind is None and multi and r["page_index"] <= NOTES_HEAD and _kind_of((r["text"] or "")[:400], _STAGE_RULES[:1]):
        return "catalog"  # 认不出种类的合订本：开头一两页写着目录、清单的，多半是这本的目录
    return kind or "other"


def stage_plan(proj: Project, scope: str = "new") -> dict:
    """分步识别的进度：第一步（地图）认出了多少，后面每一步有几页、识别了几页、还要识别哪些（按组举例）。

    每一步的页数按像素去重；“还要识别”按 scope 算（默认是还没看图识别的页）。"""
    left = {r["page_id"] for r in pick_pages(proj, scope)} if scope != "all" else None
    with proj.connect() as con:
        everything = pages_to_recognize(con, scope="all")
        hints = {**map_hints(con), **depth_targets(proj)}
        cad = con.execute("SELECT COUNT(*) AS n, SUM(f.sha256 IN (SELECT sha256 FROM cad_reads WHERE version = ?)) AS done,"
                          " SUM(f.sha256 IN (SELECT sha256 FROM cad_reads WHERE sheets > 0)) AS ok"
                          " FROM files f WHERE f.kind = 'cad' AND f.ext IN ('.dwg', '.dxf')", (dwg.INSTRUCTION_VERSION,)).fetchone()
        dwg_sheets = con.execute("SELECT COUNT(*) FROM recognitions WHERE executor = 'dwg'").fetchone()[0]
    stages = {k: {"key": k, "label": lb, "desc": d, "total": 0, "left": 0, "groups": {}} for k, lb, d in STAGES}
    numbered = skipped = 0
    rows_left = []
    for r in everything:
        g = first_guess(r)
        numbered += g is not None
        grp = stage_group(r, hints, g)
        if grp == SKIP:
            skipped += 1
            continue
        st = stages[GROUP_STAGE[grp]]
        st["total"] += 1
        if left is None or r["page_id"] in left:
            st["left"] += 1
            st["groups"].setdefault(grp, []).append(r)
            rows_left.append(r)
    out = []
    for k, _lb, _d in STAGES:
        st = stages[k]
        groups = [{"key": g, "label": GROUP_LABEL[g], "pages": len(rs),
                   "examples": list(dict.fromkeys(x["first_path"].split(" / ")[-1] for x in rs))[:3]}
                  for g, rs in sorted(st["groups"].items(), key=lambda kv: GROUP_RANK[kv[0]])]
        out.append({**st, "done": st["total"] - st["left"] if left is not None else 0, "groups": groups})
    return {
        "map": {"pages": len(everything), "numbered": numbered, "titles": len(hints["titles"]), "skipped": skipped,
                "dwg_files": cad["n"] or 0, "dwg_read": cad["done"] or 0, "dwg_ok": cad["ok"] or 0, "dwg_sheets": dwg_sheets},
        "stages": out,
        "_rows": rows_left,  # 给估算用：还要识别的页（不往外给）
        "_hints": hints,
    }


def stage_rows(rows, stages: list[str], hints: dict) -> list:
    """只留这几步的页，按步骤、组排好（同组保持文件顺序）。"""
    bad = [k for k in stages if k not in STAGE_RANK]
    if bad:
        raise ValueError(f"未知的识别步骤：{'、'.join(bad)}")
    keyed = [(GROUP_RANK[g], i, r) for i, r in enumerate(rows) for g in [stage_group(r, hints)] if GROUP_STAGE.get(g) in stages]
    return [r for _g, _i, r in sorted(keyed, key=lambda x: (x[0], x[1]))]


def pick_pages(proj: Project, scope: str = "new", priorities: list[str] | None = None,
               pages: list[str] | None = None, stages: list[str] | None = None) -> list:
    """按范围挑要识别的页面，按优先级排好；priorities 给了就只要这几档；stages 给了就只要分步识别的这几步，
    按步骤排；pages 给了就只识别这几页（按需识别）。"""
    if scope not in SCOPES:
        raise ValueError(f"未知的识别范围 {scope}，可选：{'、'.join(SCOPES)}")
    only = set(pages) if pages is not None else _conflict_pages(proj) if scope == "conflict" else None
    with proj.connect() as con:
        rows = pages_to_recognize(con, scope=scope, only=only)
        hints = map_hints(con) if stages is not None else None
    if stages is not None:
        return stage_rows(rows, stages, {**hints, **depth_targets(proj)})
    if priorities is not None:
        bad = [k for k in priorities if k not in PRIORITY_RANK]
        if bad:
            raise ValueError(f"未知的识别优先级：{'、'.join(bad)}")
        rows = [r for r in rows if priority_of(r["first_path"]) in priorities]
    return sorted(rows, key=lambda r: PRIORITY_RANK[priority_of(r["first_path"])])  # 同一档里保持文件顺序


def _conflict_pages(proj: Project) -> set[str]:
    from .state import build  # 避免循环导入
    st = build(proj, with_diff=False)
    sheets = {s["no"]: s for s in st["sheets"]}
    facts = {f["key"]: f for g in st["facts"]["groups"] for f in g["facts"]}
    out: set[str] = set()
    for i in st["issues"]:
        if i["kind"] in ("版本冲突", "改图未升版", "版本提醒") and i["target"].get("sheet") in sheets:
            out |= {v["page_id"] for v in sheets[i["target"]["sheet"]]["versions"]}
        if i["kind"] == "事实冲突" and i["target"].get("fact") in facts:
            out |= {e["page_id"] for e in facts[i["target"]["fact"]]["evidence"] if e["active"]}
    return out


def pages_to_recognize(con, include_done: bool = False, scope: str = "new", only: set[str] | None = None) -> list:
    """按范围挑选要识别的页面。像素完全相同的页面只挑一个，识别结果会被其他页面复用。"""
    if include_done:
        scope = "all"
    rows = con.execute(
        """
        SELECT p.*, f.stored_path,
               (SELECT MIN(o.path) FROM occurrences o WHERE o.sha256 = p.file_sha256) AS first_path,
               (SELECT COUNT(*) FROM pages p2 WHERE p2.file_sha256 = p.file_sha256) AS page_count
        FROM pages p JOIN files f ON f.sha256 = p.file_sha256
        WHERE f.kind = 'pdf'
        ORDER BY first_path, p.page_index
        """
    ).fetchall()
    rec_by_hash: dict[str, dict] = {}
    for r in con.execute("SELECT p.render_hash, r.data FROM pages p JOIN recognitions r ON r.page_id = p.page_id"):
        rec = json.loads(r["data"])
        old = rec_by_hash.get(r["render_hash"])
        if old is None or (old["run"]["executor"] in PLACEHOLDER_EXECUTORS and rec["run"]["executor"] not in PLACEHOLDER_EXECUTORS):
            rec_by_hash[r["render_hash"]] = rec
    out, seen = [], set()
    for r in rows:
        if r["render_hash"] in seen:
            continue
        rec = rec_by_hash.get(r["render_hash"])
        if only is not None:
            pick = r["page_id"] in only
        elif scope == "all":
            pick = True
        elif scope == "unrecognized":
            pick = rec is None
        elif scope == "weak":
            pick = rec is None or _is_weak(rec)
        else:  # new
            pick = rec is None or rec["run"]["executor"] in PLACEHOLDER_EXECUTORS
        if not pick:
            continue
        seen.add(r["render_hash"])
        out.append(r)
    return out


def make_task(proj: Project, include_done: bool = False, render: bool = True, scope: str = "new",
              priorities: list[str] | None = None, progress=None, pages: list[str] | None = None,
              stages: list[str] | None = None) -> Path | None:
    """生成识别任务包：页面按优先级排（目录、说明、平面在前），priorities 给了就只放这几档，pages 给了就只放这几页。

    渲染页面图片较慢，progress(已完成, 总数) 用来报进度。某一页取文字、出图出错（文件坏了、被删了）不放进任务包，
    记在清单的 skipped 里，别的页照常放。"""
    rows = pick_pages(proj, "all" if include_done else scope, priorities, pages, stages)
    if not rows:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name, n = stamp, 1
    while (proj.root / "recognition" / "tasks" / name).exists():
        n += 1
        name = f"{stamp}-{n}"
    task = proj.dir("recognition", "tasks", name)
    spec = resources.files("bidbench").joinpath("spec")
    (task / "任务说明.md").write_text(spec.joinpath("任务说明.md").read_text(encoding="utf-8"), encoding="utf-8")
    (task / "schema.json").write_text(spec.joinpath("schema.json").read_text(encoding="utf-8"), encoding="utf-8")
    items, skipped = [], []
    for i, r in enumerate(rows, 1):
        pdf = proj.root / r["stored_path"]
        pid = r["page_id"]
        try:
            lines = pdfpages.text_lines(pdf, r["page_index"])
            tdir = task / "text"
            tdir.mkdir(exist_ok=True)
            (tdir / f"{pid}.txt").write_text(r["text"], encoding="utf-8")
            (tdir / f"{pid}.json").write_text(json.dumps({"lines": lines}, ensure_ascii=False, indent=1), encoding="utf-8")
            if render:
                pdfpages.render_page(pdf, r["page_index"], task / "pages" / f"{pid}.png", dpi=150)
                pdfpages.render_page(pdf, r["page_index"], task / "pages" / f"{pid}-titleblock.png", dpi=300,
                                     clip_frac=TITLEBLOCK_CLIP)
        except Exception as e:  # noqa: BLE001 - 一页出不了图不能让整个任务包做不成
            log.warning("任务包里放不进 %s：%s", pid, type(e).__name__)
            skipped.append({"page_id": pid, "file_path": r["first_path"], "error": f"{type(e).__name__}: {e}"})
            if progress:
                progress(i, len(rows))
            continue
        items.append({
            "page_id": pid,
            "priority": priority_of(r["first_path"]),
            "source": {
                "file_path": r["first_path"],
                "file_sha256": r["file_sha256"],
                "page_index": r["page_index"],
                "page_count": r["page_count"],
                "has_text_layer": bool(r["has_text"]),
            },
            "image": f"pages/{pid}.png" if render else None,
            "titleblock_image": f"pages/{pid}-titleblock.png" if render else None,
            "text": f"text/{pid}.txt",
            "text_lines": f"text/{pid}.json",
        })
        if progress:
            progress(i, len(rows))
    manifest = {"created_at": now_iso(), "project": proj.meta["name"], "scope": scope, "pages": items}
    if skipped:
        manifest["skipped"] = skipped
    (task / "manifest_in.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (task / "results" / "pages").mkdir(parents=True, exist_ok=True)
    return task


@dataclass
class ImportResult:
    imported: int = 0
    unchanged: int = 0
    errors: list = field(default_factory=list)


NOT_RESULTS = ("manifest.json", "manifest_in.json", "usage.json")


def result_files(results_dir: Path) -> list[Path]:
    return [f for f in sorted(Path(results_dir).rglob("*.json")) if f.name not in NOT_RESULTS]


def import_results(proj: Project, results_dir: Path, files: list[Path] | None = None, report: bool = True) -> ImportResult:
    """校验并导入识别结果。不通过校验的文件一律不导入，列入问题清单。

    files 给了就只导这几个（识别途中分批导入用）；report=False 时不写问题清单，由调用方汇总后再写。"""
    v = _validator()
    res = ImportResult()
    files = result_files(results_dir) if files is None else files
    with proj.connect() as con:
        known = {r[0] for r in con.execute("SELECT page_id FROM pages")}
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                res.errors.append({"file": str(f), "error": f"不是合法 JSON：{e}"})
                continue
            errs = sorted(v.iter_errors(data), key=lambda e: list(e.absolute_path))
            if errs:
                e = errs[0]
                loc = "/".join(str(x) for x in e.absolute_path) or "(根)"
                res.errors.append({"file": str(f), "error": f"{loc}: {e.message}（共 {len(errs)} 处）"})
                continue
            pid = data["page_id"]
            if pid not in known:
                res.errors.append({"file": str(f), "error": f"page_id {pid} 不属于本项目"})
                continue
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            old = con.execute("SELECT data FROM recognitions WHERE page_id=?", (pid,)).fetchone()
            if old and old["data"] == payload:
                res.unchanged += 1
                continue
            run = data["run"]
            con.execute(
                "INSERT OR REPLACE INTO recognitions(page_id, executor, model, instruction_version, finished_at, imported_at, data)"
                " VALUES (?,?,?,?,?,?,?)",
                (pid, run["executor"], run["model"], run["instruction_version"], run["finished_at"], now_iso(), payload),
            )
            res.imported += 1
    if report and res.errors:
        write_import_report(proj, res.errors)
    return res


def write_import_report(proj: Project, errors: list[dict]) -> None:
    out = proj.path("reports", "识别结果导入问题.md")
    lines = ["# 识别结果导入问题", "", f"共 {len(errors)} 个文件未导入，修正后重新运行 `bidbench import`。", "",
             "| 文件 | 问题 |", "| --- | --- |"]
    lines += [f"| {e['file']} | {e['error']} |" for e in errors]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def latest_task(proj: Project) -> Path | None:
    d = proj.root / "recognition" / "tasks"
    if not d.exists():
        return None
    tasks = sorted(p for p in d.iterdir() if p.is_dir())
    return tasks[-1] if tasks else None


def copy_spec(dst: Path) -> None:
    spec = resources.files("bidbench").joinpath("spec")
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("任务说明.md", "schema.json"):
        shutil.copyfile(str(spec.joinpath(name)), dst / name)


def page_markdown(r: dict) -> str:
    """单页识别结果的 Markdown 版本（L2 图纸详情）。"""
    tb = r["title_block"]
    v = lambda k: tb[k]["value"] or "（未识别）"  # noqa: E731
    out = [f"# {v('drawing_no')} {v('drawing_title')}", "",
           f"- 页面类型：{r['page_kind']}", f"- 版次：{v('revision')}　日期：{v('issue_date')}　比例：{v('scale')}",
           f"- 出处：{r['source']['file_path']} 第 {r['source']['page_index']} 页", "", r["content"]["summary"], ""]
    if r["references"]:
        out += ["## 引用", ""] + [f"- {x['raw_text']}（{x['ref_type']}{'，编号 ' + x['mark'] if x.get('mark') else ''}）"
                                  for x in r["references"]] + [""]
    if r["content"].get("details"):
        out += ["## 本图的详图", ""] + [f"- {x['mark']} {x['title'] or ''} {x['scale'] or ''}".rstrip() for x in r["content"]["details"]] + [""]
    cov = {k: (v or {}).get("value") for k, v in (r["content"].get("coverage") or {}).items()}
    if any(cov.values()):
        out += [f"- 管哪一块：{'　'.join(f'{lab} {cov[k]}' for k, lab in (('zone', '分区'), ('axis_x', '数字轴'), ('axis_y', '字母轴')) if cov.get(k))}", ""]
    if r["catalog_entries"]:
        out += ["## 目录", "", "| 图号 | 图名 | 版次 |", "| --- | --- | --- |"]
        out += [f"| {x['drawing_no']} | {x['drawing_title']} | {x['revision']} |" for x in r["catalog_entries"]] + [""]
    if r["content"]["key_values"]:
        out += ["## 关键数值", ""] + [f"- {x['name']}：{x['value']} {x['unit'] or ''}".rstrip() for x in r["content"]["key_values"]] + [""]
    if r["content"]["notes"]:
        out += ["## 说明", ""] + [f"- {x['label']} {x['text']}" for x in r["content"]["notes"]] + [""]
    if r["issues"]:
        out += ["## 问题", ""] + [f"- {x['type']}：{x['description']}" for x in r["issues"]] + [""]
    return "\n".join(out)
