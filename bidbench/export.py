"""导出：现行图纸文件夹（给人和建模用）、数据镜像（给 agent 读）。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path

from . import navigate, pdfpages, risk
from . import state as statemod
from .recognition import page_markdown
from .state import CONF_LABEL, STATUS_LABEL
from .store import Project


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip()


def _page_source(proj: Project, page_id: str) -> tuple[Path, int, int, bool]:
    with proj.connect() as con:
        r = con.execute(
            "SELECT p.page_index, p.file_sha256, f.stored_path, f.page_count FROM pages p JOIN files f ON f.sha256=p.file_sha256"
            " WHERE p.page_id=?", (page_id,)).fetchone()
        unsafe = risk.needs_cleaning(con, r["file_sha256"])
    return proj.root / r["stored_path"], r["page_index"], r["page_count"], unsafe


def page_pdf_bytes(proj: Project, page_id: str) -> bytes:
    """单页 PDF。原文件带脚本等风险内容时，给出去掉这些内容的副本。"""
    path, idx, count, unsafe = _page_source(proj, page_id)
    if unsafe:
        return pdfpages.clean_pdf(path, idx if count > 1 else None)
    return path.read_bytes() if count == 1 else pdfpages.single_page_pdf(path, idx)


def page_png_bytes(proj: Project, page_id: str) -> bytes:
    """单页图片（只是渲染，不执行文件里的脚本）。"""
    path, idx, _count, _unsafe = _page_source(proj, page_id)
    return pdfpages.page_png(path, idx)


def export_current(proj: Project, state: dict, target: Path | None = None) -> Path:
    """只放现行版。合订本里的图单独抽成一页 PDF。建模只从这里取图，就接触不到旧版。"""
    target = target or proj.root / "exports" / f"现行图纸-批次{state['as_of']:02d}-{date.today():%Y%m%d}"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    rows = ["# 现行图纸清单", "", f"截至批次 #{state['as_of']}。只含现行版；冲突、待确认的图已标出。", "",
            "| 专业 | 图号 | 图名 | 版次 | 状态 | 文件 |", "| --- | --- | --- | --- | --- | --- |"]
    for s in state["sheets"]:
        if s["status"] in ("missing", "voided", "guess") or not s["current_page"]:
            continue  # 按文件名暂定的图还没看图识别，不算现行图纸
        flag = "" if s["status"] in ("current", "updated", "new") else f"【{STATUS_LABEL[s['status']]}】"
        name = _safe(f"{flag}{s['no']} {s['title'] or ''} {s['current_rev']}版") + ".pdf"
        d = target / s["discipline"]
        d.mkdir(exist_ok=True)
        (d / name).write_bytes(page_pdf_bytes(proj, s["current_page"]))
        cleaned = "（原文件带脚本，已去除）" if _page_source(proj, s["current_page"])[3] else ""
        rows.append(f"| {s['discipline']} | {s['no']} | {s['title'] or ''} | {s['current_rev']} | {STATUS_LABEL[s['status']]} | {s['discipline']}/{name}{cleaned} |")
    missing = [s for s in state["sheets"] if s["status"] == "missing"]
    if missing:
        rows += ["", "## 缺失图纸", ""] + [f"- {s['no']} {s['title'] or ''}：{'；'.join(s['notes'])}" for s in missing]
    guessed = [s for s in state["sheets"] if s["status"] == "guess"]
    if guessed:
        rows += ["", f"## 按文件名暂定、还没看图识别的图（{len(guessed)} 张，没有导出）", ""]
        rows += [f"- {s['no']} {s['title'] or ''} {s['current_rev'] or ''}".rstrip() for s in guessed]
    (target / "现行图纸清单.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return target


def export_mirror(proj: Project, state: dict, target: Path | None = None) -> Path:
    """面向 agent 的数据镜像：事实层 → L0 项目地图 → L1 图纸卡片 → L2 图纸详情，逐层往下钻。"""
    target = target or proj.root / "exports" / "mirror"
    if target.exists():
        shutil.rmtree(target)
    (target / "L1-图纸卡片").mkdir(parents=True)
    (target / "L2-图纸详情").mkdir()
    with proj.connect() as con:
        recs = {r["page_id"]: json.loads(r["data"]) for r in con.execute("SELECT page_id, data FROM recognitions")}
        render = {r["page_id"]: r["render_hash"] for r in con.execute("SELECT page_id, render_hash FROM pages")}
    by_hash = {}
    for pid, rec in recs.items():
        by_hash.setdefault(render.get(pid), rec)

    (target / "README.md").write_text(
        "# 数据镜像使用说明（给 agent）\n\n"
        "按需逐层读取，不要一次读完：\n\n"
        "1. `事实.md` / `facts.json`：交叉验证后的项目事实，带置信度和出处。接到任务先读这里。\n"
        "2. `L0-项目地图.md`：整套图纸概况、批次、问题。\n"
        "3. `L1-图纸卡片/<图号>.md`：每张图的图签、现行版次、版本链、关联图纸。\n"
        "4. `L2-图纸详情/<图号>-<版次>.md`：识别出的全部内容。\n"
        "5. 需要核对原图时，用 L1 卡片里的页面编号回到原文件。\n"
        "6. `relations.json`：图纸之间的全部关系，给程序读。`sheets` 是每张图（状态、现行版次、页面编号、楼层、文件），"
        "`relations` 是有出处的关系（引用、跨专业引用、目录列出、变更单，带原文、页面编号和位置 bbox），"
        "`same_floor`、`general` 是每张图的同层图纸、通用图。按任务找图时：先按楼层、专业挑出主图，"
        "再顺着 relations 找它引用的图，最后看 general。\n\n"
        "规则：默认只用现行版。状态为冲突、待确认、缺失的内容不要当作确定事实使用。\n",
        encoding="utf-8")
    (target / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    (target / "facts.json").write_text(json.dumps(state["facts"], ensure_ascii=False, indent=1), encoding="utf-8")
    from .reports import facts_report
    (target / "事实.md").write_text(facts_report(state), encoding="utf-8")

    by_disc: dict[str, list] = {}
    for s in state["sheets"]:
        by_disc.setdefault(s["discipline"], []).append(s)
    l0 = [f"# {state['project']['name']}：项目地图", "",
          f"截至批次 #{state['as_of']}，共 {len(state['sheets'])} 个图号。", "", "## 收图批次", ""]
    l0 += [f"- 批次 #{b['seq']} {b['received_at']} {b['archive']}：{'、'.join(b['kinds'])}" for b in state["batches"]]
    l0 += ["", "## 各专业图纸", ""]
    for disc, items in by_disc.items():
        l0.append(f"- {disc}（{len(items)}）：" + "，".join(f"{s['no']}{'【' + STATUS_LABEL[s['status']] + '】' if s['status'] != 'current' else ''}" for s in items))
    if state.get("floor_coverage"):
        l0 += ["", "## 楼层覆盖", ""] + [
            f"- {r['group']}：" + ("；".join(f"{c['floors']}（{c['sheets'][0]}）" for c in r["covered"]) or "无楼层信息")
            + (f"；缺 {r['missing_text']}" if r["missing"] else "")
            + "".join(f"；{x}" for x in r.get("remarks", [])) for r in state["floor_coverage"]]
    l0 += ["", "## 待处理问题", ""] + [f"- [{i['kind']}] {i['title']}：{i['detail']}" for i in state["issues"]]
    (target / "L0-项目地图.md").write_text("\n".join(l0) + "\n", encoding="utf-8")

    around = {s["no"]: statemod.same_floor(state, s["no"]) for s in state["sheets"]}
    (target / "relations.json").write_text(json.dumps(relations_doc(state, around), ensure_ascii=False, indent=1),
                                           encoding="utf-8")

    edges_from: dict[str, list] = {}
    for e in state["edges"]:
        edges_from.setdefault(e["from"], []).append(e)
        edges_from.setdefault(e["to"], []).append(e)
    facts_by_sheet: dict[str, list] = {}
    for g in state["facts"]["groups"]:
        for f in g["facts"]:
            for src in f["sources"]:
                facts_by_sheet.setdefault(src, []).append(f)
    for s in state["sheets"]:
        card = [f"# {s['no']} {s['title'] or ''}", "", f"- 专业：{s['discipline']}", f"- 状态：{STATUS_LABEL[s['status']]}",
                f"- 现行版次：{s['current_rev'] or '—'}", f"- 页面编号：{s['current_page'] or '—'}"]
        if s["paths"]:
            card.append(f"- 出处：{s['paths'][0]}")
        if s["notes"]:
            card += ["", "## 注意", ""] + [f"- {n}" for n in s["notes"]]
        if s["versions"]:
            card += ["", "## 版本链", ""] + [f"- {v['rev']} 版 {v['date'] or ''} 批次#{v['batch']}：{v['state']}" for v in s["versions"]]
        rel = edges_from.get(s["no"], [])
        if rel:
            card += ["", "## 关联图纸", ""] + [
                f"- {'→ ' + e['to'] if e['from'] == s['no'] else '← ' + e['from']}：{e['label']}" for e in rel]
        near = around[s["no"]]
        if near["same_floor"]:
            card += ["", f"## 同层图纸（{near['floors']}）", ""] + [
                f"- {t['no']} {t['title'] or ''}（{t['discipline']}，{t['floors']}，{t['status']}）"
                + (f"：{'；'.join(t['problems'])}" if t["problems"] else "") for t in near["same_floor"][:30]]
            if len(near["same_floor"]) > 30:
                card.append(f"- 还有 {len(near['same_floor']) - 30} 张，见 relations.json")
        if near["general"]:
            card += ["", "## 通用图（设计说明等，建哪一层都要看）", ""] + [
                f"- {t['no']} {t['title'] or ''}（{t['discipline']}，{t['status']}）" for t in near["general"]]
        if s.get("cad"):
            card += ["", "## CAD 文件", ""] + [f"- {p}" for p in s["cad"]]
        fs = facts_by_sheet.get(s["no"], [])
        if fs:
            card += ["", "## 来自此图的事实", ""] + [f"- {f['name']}：{f['value']}（{CONF_LABEL[f['conf']]}）" for f in fs]
        (target / "L1-图纸卡片" / f"{_safe(s['no'])}.md").write_text("\n".join(card) + "\n", encoding="utf-8")
        for v in s["versions"]:
            rec = recs.get(v["page_id"]) or by_hash.get(render.get(v["page_id"]))
            if not rec:
                continue
            (target / "L2-图纸详情" / f"{_safe(s['no'])}-{_safe(v['rev'])}-批次{v['batch']}.md").write_text(
                f"> 版本状态：{v['state']}\n\n" + page_markdown(rec), encoding="utf-8")
    return target


def relations_doc(state: dict, around: dict | None = None) -> dict:
    """relations.json：图纸之间的全部关系，给程序读（和 MCP 的 related_sheets 是同一份）。"""
    around = around or {s["no"]: statemod.same_floor(state, s["no"]) for s in state["sheets"]}
    sheets = {}
    for s in state["sheets"]:
        row = navigate.sheet_row(s)
        row.pop("no")
        row.update({"floor_list": s.get("floors") or [], "general": bool(s.get("general"))})
        sheets[s["no"]] = row
    return {
        "as_of_batch": state["as_of"],
        "note": "关系种类见 relation_types；from、to 是图号（多栋楼写成“塔楼 · 图号”），变更单的 from 是变更单编号。"
                "bbox 是 [x0, y0, x1, y1]，相对整页宽高 0~1，原点左上角；page_id 是关系画在哪一页。"
                "楼层 floor_list：1 是 1F，负数是地下，1000 是屋顶。",
        "relation_types": navigate.REL_NOTE, "forms": navigate.FORM_NOTE,
        "sheets": sheets,
        "relations": navigate.relations(state),
        "notices": [{"id": n["id"], "title": n["title"], "kind": n["kind"], "page_id": n["page_id"], "batch": n["batch"],
                     "changes": n["affects"]} for n in state["notices"]],
        "details": {s["no"]: [{k: d[k] for k in ("mark", "title", "scale", "page_id", "bbox")} for d in s["details"]]
                    for s in state["sheets"] if s.get("details")},
        "same_floor": {no: [t["no"] for t in a["same_floor"]] for no, a in around.items() if a["same_floor"]},
        "general": {no: [t["no"] for t in a["general"]] for no, a in around.items() if a["general"]},
        "shared_xrefs": navigate.shared_xrefs(state),
    }
