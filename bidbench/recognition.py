"""识别：生成任务包、导入结果。

执行器（外部 agent、内置 agent、模拟执行器）只看任务包、只写 results/，
系统负责校验和导入，所以执行器可以随时替换。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

import jsonschema

from . import pdfpages
from .store import Project, now_iso

TITLEBLOCK_CLIP = (0.62, 0.70, 1.0, 1.0)  # 图签常见位置：右下角


def load_schema() -> dict:
    return json.loads(resources.files("bidbench").joinpath("spec/schema.json").read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(load_schema())


def pages_to_recognize(con, include_done: bool = False) -> list:
    """需要识别的页面：没有识别结果，且没有像素完全相同、已识别过的页面可以复用。"""
    rows = con.execute(
        """
        SELECT p.*, f.stored_path,
               (SELECT MIN(o.path) FROM occurrences o WHERE o.sha256 = p.file_sha256) AS first_path,
               (SELECT COUNT(*) FROM pages p2 WHERE p2.file_sha256 = p.file_sha256) AS page_count
        FROM pages p JOIN files f ON f.sha256 = p.file_sha256
        ORDER BY first_path, p.page_index
        """
    ).fetchall()
    done_hashes = {r["render_hash"] for r in con.execute(
        "SELECT p.render_hash FROM pages p JOIN recognitions r ON r.page_id = p.page_id")}
    done_ids = {r[0] for r in con.execute("SELECT page_id FROM recognitions")}
    out, seen = [], set()
    for r in rows:
        if not include_done and (r["page_id"] in done_ids or r["render_hash"] in done_hashes):
            continue
        if r["render_hash"] in seen:  # 同一批任务里像素相同的页面只识别一次
            continue
        seen.add(r["render_hash"])
        out.append(r)
    return out


def make_task(proj: Project, include_done: bool = False, render: bool = True) -> Path | None:
    with proj.connect() as con:
        rows = pages_to_recognize(con, include_done)
    if not rows:
        return None
    task = proj.dir("recognition", "tasks", datetime.now().strftime("%Y%m%d-%H%M%S"))
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
    (task / "manifest_in.json").write_text(
        json.dumps({"created_at": now_iso(), "project": proj.meta["name"], "pages": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (task / "results" / "pages").mkdir(parents=True, exist_ok=True)
    return task


@dataclass
class ImportResult:
    imported: int = 0
    unchanged: int = 0
    errors: list = field(default_factory=list)


def import_results(proj: Project, results_dir: Path) -> ImportResult:
    """校验并导入识别结果。不通过校验的文件一律不导入，列入问题清单。"""
    v = _validator()
    res = ImportResult()
    files = sorted(Path(results_dir).rglob("*.json"))
    with proj.connect() as con:
        known = {r[0] for r in con.execute("SELECT page_id FROM pages")}
        for f in files:
            if f.name in ("manifest.json", "manifest_in.json"):
                continue
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
    if res.errors:
        out = proj.path("reports", "识别结果导入问题.md")
        lines = ["# 识别结果导入问题", "", f"共 {len(res.errors)} 个文件未导入，修正后重新运行 `bidbench import`。", "",
                 "| 文件 | 问题 |", "| --- | --- |"]
        lines += [f"| {e['file']} | {e['error']} |" for e in res.errors]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return res


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
        out += ["## 引用", ""] + [f"- {x['raw_text']}（{x['ref_type']}）" for x in r["references"]] + [""]
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
