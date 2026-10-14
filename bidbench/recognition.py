"""识别：生成任务包、导入结果。

执行器（外部 agent、内置 agent、模拟执行器）只看任务包、只写 results/，
系统负责校验和导入，所以执行器可以随时替换。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

import jsonschema

from . import pdfpages
from .store import PLACEHOLDER_EXECUTORS, Project, now_iso

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
    ("notes", re.compile(r"说明|图例|general notes?|\bnotes?\b|\bspecifications?\b|\blegends?\b|\babbreviations?\b", re.I)),
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
        name = re.sub(r"[_\-.]+", " ", name)  # Level_01-GA_Plan 这类文件名按词切开
        for key, rx in _PRIORITY_RULES:
            if rx.search(name):
                return key
    return "other"


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


def pick_pages(proj: Project, scope: str = "new", priorities: list[str] | None = None,
               pages: list[str] | None = None) -> list:
    """按范围挑要识别的页面，按优先级排好；priorities 给了就只要这几档；pages 给了就只识别这几页（按需识别）。"""
    if scope not in SCOPES:
        raise ValueError(f"未知的识别范围 {scope}，可选：{'、'.join(SCOPES)}")
    only = set(pages) if pages is not None else _conflict_pages(proj) if scope == "conflict" else None
    with proj.connect() as con:
        rows = pages_to_recognize(con, scope=scope, only=only)
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
              priorities: list[str] | None = None, progress=None, pages: list[str] | None = None) -> Path | None:
    """生成识别任务包：页面按优先级排（目录、说明、平面在前），priorities 给了就只放这几档，pages 给了就只放这几页。

    渲染页面图片较慢，progress(已完成, 总数) 用来报进度。"""
    rows = pick_pages(proj, "all" if include_done else scope, priorities, pages)
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
    items = []
    for r in rows:
        pdf = proj.root / r["stored_path"]
        pid = r["page_id"]
        lines = pdfpages.text_lines(pdf, r["page_index"])
        tdir = task / "text"
        tdir.mkdir(exist_ok=True)
        (tdir / f"{pid}.txt").write_text(r["text"], encoding="utf-8")
        (tdir / f"{pid}.json").write_text(json.dumps({"lines": lines}, ensure_ascii=False, indent=1), encoding="utf-8")
        if render:
            pdfpages.render_page(pdf, r["page_index"], task / "pages" / f"{pid}.png", dpi=150)
            pdfpages.render_page(pdf, r["page_index"], task / "pages" / f"{pid}-titleblock.png", dpi=300,
                                 clip_frac=TITLEBLOCK_CLIP)
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
            progress(len(items), len(rows))
    (task / "manifest_in.json").write_text(
        json.dumps({"created_at": now_iso(), "project": proj.meta["name"], "scope": scope, "pages": items},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
