"""AI 助手的工具层：查询全部只读；改动只能“提议”，由人确认后才生效。

MCP 服务（mcp_server.py）和工作台的 AI 助手面板（chat.py）都调用这里的函数，规则只写一处：
- 查询工具返回精简的结构化数据，每条结论都带出处（图号、版次、页面编号）
- 提议工具只写入 proposals 表；确认 / 驳回只由人通过工作台或命令行完成，不对 AI 开放
- 助手觉得工作台本身该改进时，也只能提议（改进建议）；确认后记进本机的改进建议清单，由开发者按清单改代码、开 PR，
  助手自己碰不到代码和 GitHub
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf

from . import appconfig, classify, export, guide, navigate, numbering, recognition, reports, state
from .recognition import page_markdown
from .state import CONF_LABEL, STATUS_LABEL
from .store import Project, add_decision, now_iso

AI_SOURCE = "AI 助手"
SUGGESTIONS = "改进建议.jsonl"  # 在用户目录（appconfig.config_dir()），不在项目里：建议针对的是软件，不是项目
CONFIRMED_ACTOR = "AI 助手建议，我确认"
MAX_LIST = 200
MAX_BASIS = 200  # check_basis 一次核对的构件数
BASIS_SKIP = {"基线过期", "风险文件", "解包问题"}  # 和图上内容靠不靠得住无关的问题，核对依据时不提


class ToolError(Exception):
    """参数不对或对象不存在。消息会原样返回给 AI。"""


class Toolkit:
    def __init__(self, proj: Project, source: str = AI_SOURCE):
        self.proj = proj
        self.source = source
        self._cache: dict = {}

    # ---------------- 内部 ----------------

    def _stamp(self):
        db = self.proj.db_path
        return db.stat().st_mtime_ns if db.exists() else 0

    def _state(self, as_of: int | None = None) -> dict:
        key = (as_of, self._stamp())
        if key not in self._cache:
            self._cache = {key: state.build(self.proj, as_of)}
        return self._cache[key]

    def _sheet(self, st: dict, no: str) -> dict:
        n = (no or "").strip()
        if "·" in n:  # 带塔楼的写法：“T1 · 建施-01”
            tw, _, num = n.partition("·")
            n = f"{tw.strip()}{state.SEP}{state.norm_no(num)}"
        else:
            n = state.norm_no(n)
        for s in st["sheets"]:
            if s["no"] == n:
                return s
        same = [s for s in st["sheets"] if s.get("number") == n]
        if len(same) == 1:
            return same[0]
        if same:
            raise ToolError(f"图号 {no} 在几栋楼都有：{'、'.join(s['no'] for s in same)}。请带上塔楼，例如“{same[0]['no']}”。")
        # 以前按文件名暂定时记下的图号（PRJ-3036），图签读出来是 S-3036：只有一张图的文件名是它时就是这张
        cached = getattr(self, "_file_nos", None)
        if cached is None or cached[0] is not st:
            cached = self._file_nos = (st, {s["no"]: state.file_no(s.get("paths")) for s in st["sheets"] if s["status"] != "guess"})
        by_file = [s for s in st["sheets"] if cached[1].get(s["no"]) == n]
        if len(by_file) == 1:
            return by_file[0]
        raise ToolError(f"没有图号 {no}。可以先用 list_sheets 查看全部图号。")

    def _facts(self, st: dict) -> list[dict]:
        return [dict(f, group=g["label"]) for g in st["facts"]["groups"] for f in g["facts"]]

    # ---------------- 查询：全局 ----------------

    def project_overview(self) -> dict:
        st = self._state()
        counts: dict[str, int] = {}
        for s in st["sheets"]:
            counts[STATUS_LABEL[s["status"]]] = counts.get(STATUS_LABEL[s["status"]], 0) + 1
        levels = {"crit": "严重", "warn": "提醒", "info": "信息"}
        return {
            "project": st["project"]["name"],
            "as_of_batch": st["as_of"],
            "batches": [{"seq": b["seq"], "received_at": b["received_at"], "archive": b["archive"],
                         "kinds": b["kinds"], "sheets": b["sheets"]} for b in st["batches"]],
            "sheet_count": len(st["sheets"]),
            "sheets_by_status": counts,
            "issues_by_level": {levels[k]: sum(1 for i in st["issues"] if i["level"] == k) for k in levels},
            "top_issues": [f"[{i['kind']}] {i['title']}" for i in st["issues"] if i["level"] != "info"][:15],
            "floor_coverage": [f"{r['group']}：{reports.FLOOR_STATUS[r['status']]}"
                               + (f"，缺 {r['missing_text']}" if r["missing"] else "") for r in st["floor_coverage"]],
            "recognition": st["recognition"],
            "risk_files_pending": st["security"]["pending"],
            "pending_proposals": len(st["proposals"]),
            "notes": [f"{n['topic']}：{n['text']}" for n in st["notes"]],
            "next_steps": [f"{t['title']}：{t['text']}" for t in st["guide"]["tasks"]][:4],
            "hint": "逐层往下查：list_sheets / get_sheet 看图纸，list_facts / get_fact 看事实，list_issues 看问题。"
                    "用户问下一步做什么时，按 next_steps 的顺序说，第一条就是工作台问题中心最上面的“下一步”。",
        }

    def list_batches(self) -> list[dict]:
        st = self._state()
        return [{"seq": b["seq"], "name": b["name"], "received_at": b["received_at"], "archive": b["archive"],
                 "kinds": b["kinds"], "confidence": b["confidence"], "sheets": b["sheets"], "files": b["files"],
                 "extraction_issues": [f"{i['kind']}：{i['path']}" for i in b["issues"]]} for b in st["batches"]]

    def batch_diff(self, batch: int | None = None) -> dict:
        st = self._state(batch)
        if not st["as_of"]:
            raise ToolError("还没有收图批次。")
        return {"batch": st["as_of"], "report": reports.diff_report(st)}

    def compare_batches(self, from_batch: int, to_batch: int) -> dict:
        a, b = self._state(from_batch), self._state(to_batch)
        if not a["as_of"] or not b["as_of"]:
            raise ToolError("批次号不存在。")
        d = state.diff_states(a, b)
        d["from"], d["to"] = a["as_of"], b["as_of"]
        return d

    # ---------------- 查询：图纸 ----------------

    def list_sheets(self, discipline: str | None = None, status: str | None = None,
                    keyword: str | None = None, limit: int = 100) -> dict:
        st = self._state()
        rev_status = {v: k for k, v in STATUS_LABEL.items()}
        want = rev_status.get(status, status) if status else None
        rows = []
        for s in st["sheets"]:
            if discipline and s["discipline"] != discipline:
                continue
            if want and s["status"] != want:
                continue
            if keyword and keyword not in s["no"] and keyword not in (s["title"] or ""):
                continue
            rows.append({"no": s["no"], "title": s["title"], "discipline": s["discipline"],
                         "status": STATUS_LABEL[s["status"]], "current_rev": s["current_rev"],
                         "page_id": s["current_page"], **({"tower": s["tower"]} if s.get("tower") else {})})
        limit = max(1, min(limit, MAX_LIST))
        return {"total": len(rows), "sheets": rows[:limit], "truncated": len(rows) > limit,
                "statuses": list(STATUS_LABEL.values())}

    def get_sheet(self, no: str) -> dict:
        st = self._state()
        s = self._sheet(st, no)
        refs = [{"direction": "引用" if e["from"] == s["no"] else "被引用",
                 "sheet": e["to"] if e["from"] == s["no"] else e["from"], "text": e["label"], "form": e.get("form"),
                 **({"mark": e["mark"]} if e.get("mark") else {}),
                 **({"jump_to": e["target"]} if e.get("target") and e["from"] == s["no"] else {})}
                for e in st["edges"] if s["no"] in (e["from"], e["to"])]
        facts = []
        for f in self._facts(st):
            if s["no"] not in f["sources"]:
                continue
            own = next((e for e in f["evidence"] if e["active"] and e["sheet"] == s["no"]), None)
            facts.append({"key": f["key"], "name": f["name"], "this_sheet": (own["text"] or own["value"]) if own else None,
                          "project_value": f["value"], "confidence": CONF_LABEL[f["conf"]],
                          **({"changed_by": f["notices"]} if f.get("notices") else {})})
        issues = [{"id": i["id"], "kind": i["kind"], "title": i["title"], "detail": i["detail"],
                   "relation": "本图" if i["target"].get("sheet") == s["no"] else "相关"}
                  for i in st["issues"] if i["target"].get("sheet") == s["no"] or s["no"] in i.get("sheets", [])]
        around = state.same_floor(st, s["no"], limit=40)
        notices = [{"id": n["id"], "title": n["title"], "kind": n["kind"], "batch": n["batch"], "page_id": n["page_id"],
                    "also_changes": [x for x in n["affects"] if x != s["no"]]}
                   for n in st["notices"] if s["no"] in n["affects"]]
        return {"no": s["no"], "tower": s.get("tower"), "title": s["title"], "discipline": s["discipline"],
                "status": STATUS_LABEL[s["status"]],
                "current_rev": s["current_rev"], "current_page_id": s["current_page"], "paths": s["paths"],
                "notes": s["notes"], "notices": notices,
                "versions": [{"rev": v["rev"], "date": v["date"], "batch": v["batch"], "state": v["state"],
                              "page_id": v["page_id"]} for v in s["versions"]],
                "references": refs, "floors": s["floors_text"], "covers": navigate.coverage_text(s),
                "details": [{k: d[k] for k in ("mark", "title", "scale", "page_id", "bbox")} for d in s.get("details") or ()], "same_floor": around["same_floor"],
                **({"more_same_floor": around["more_same_floor"]} if "more_same_floor" in around else {}),
                "general_sheets": around["general"], "own_values": s["own_values"],
                "recognition_notes": s["rec_notes"], "facts": facts, "issues": issues,
                "fields": {k: {"value": f["value"], "confidence": CONF_LABEL[f["conf"]], "source": f["src"],
                               **({"raw": f["raw"]} if f["raw"] else {})} for k, f in (s.get("fields") or {}).items() if f},
                **({"guess_confidence": CONF_LABEL[s["conf"]],
                    "guess_note": "还没看图识别，fields 是按文件名、文件夹推测的，把握见各项的 confidence"} if s["status"] == "guess" else {}),
                **({"files_not_recognized_yet": s["pending_files"]} if s.get("pending_files") else {})}

    def get_page(self, page_id: str, include_text_layer: bool = False) -> dict:
        if not re.fullmatch(r"[0-9a-f]{12}-p\d{3}", page_id or ""):
            raise ToolError("page_id 格式应为 12 位十六进制加 -p 和三位页码，例如 3fa9c01b77de-p001。")
        with self.proj.connect() as con:
            page = con.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()
            if not page:
                raise ToolError(f"没有页面 {page_id}。")
            rec = con.execute("SELECT data FROM recognitions WHERE page_id=?", (page_id,)).fetchone()
            if rec is None:
                rec = con.execute("SELECT r.data FROM recognitions r JOIN pages p ON p.page_id=r.page_id"
                                  " WHERE p.render_hash=? LIMIT 1", (page["render_hash"],)).fetchone()
        out = {"page_id": page_id}
        if rec:
            data = json.loads(rec["data"])
            out.update({"recognized_by": data["run"]["executor"], "detail": page_markdown(data)})
        else:
            out["detail"] = "这一页还没有识别结果。"
        if include_text_layer:
            text = page["text"]
            out["text_layer"] = text[:6000] + ("\n……（文字层过长，已截断）" if len(text) > 6000 else "")
        return out

    def search(self, text: str, current_only: bool = True, limit: int = 20) -> dict:
        """在识别结果和 PDF 文字层里找关键词。默认只搜现行版。"""
        if not text or not text.strip():
            raise ToolError("请给出要搜索的文字。")
        st = self._state()
        page_sheet = {}
        for s in st["sheets"]:
            for v in s["versions"]:
                page_sheet[v["page_id"]] = (s["no"], s["title"], v["rev"], v["state"])
        q = text.strip()
        hits = []
        with self.proj.connect() as con:
            recs = {r["page_id"]: r["data"] for r in con.execute("SELECT page_id, data FROM recognitions")}
            for p in con.execute("SELECT page_id, text, render_hash FROM pages"):
                if p["page_id"] not in page_sheet:
                    continue
                no, title, rev, vstate = page_sheet[p["page_id"]]
                if current_only and vstate != "现行":
                    continue
                hay = p["text"] + "\n" + (recs.get(p["page_id"]) or "")
                i = hay.find(q)
                if i < 0:
                    continue
                snippet = hay[max(0, i - 40): i + len(q) + 60].replace("\n", " ")
                hits.append({"sheet": no, "title": title, "rev": rev, "version_state": vstate,
                             "page_id": p["page_id"], "snippet": snippet})
        limit = max(1, min(limit, MAX_LIST))
        return {"query": q, "total": len(hits), "hits": hits[:limit]}

    # ---------------- 查询：建模找图 ----------------

    def find_sheets_for_task(self, floor: str | None = None, discipline: str | None = None, keyword: str | None = None,
                             tower: str | None = None, limit: int = 40) -> dict:
        try:
            out = navigate.find_for_task(self._state(), floor, discipline, keyword, tower, limit)
        except ValueError as e:
            raise ToolError(str(e)) from None
        out["hint"] = ("先看“主图”，再按需看“通用图”“引用到的图”；missing 里的图没发来，建模缺资料要告诉用户（可以列进答疑）。"
                       "notices 里的变更单改过这些图，相关内容以变更单为准。要顺着某张图再往外找，用 related_sheets；"
                       "要看图上某句话、某个详图索引在哪，用 locate(page_id, bbox)。")
        return out

    def related_sheets(self, no: str, depth: int = 1, types: list[str] | None = None, direction: str = "both") -> dict:
        st = self._state()
        n = (no or "").strip()
        start = n if any(x["id"] == n for x in st["notices"]) else self._sheet(st, n)["no"]
        try:
            return navigate.walk(st, start, depth, types, direction)
        except ValueError as e:
            raise ToolError(str(e)) from None

    def locate(self, page_id: str, bbox: list[float] | None = None) -> dict:
        """一页在哪个文件第几页、属于哪张图哪一版；给了 bbox 再读出那块地方的文字、那里画的引用。"""
        if not re.fullmatch(r"[0-9a-f]{12}-p\d{3}", page_id or ""):
            raise ToolError("page_id 格式应为 12 位十六进制加 -p 和三位页码，例如 3fa9c01b77de-p001。")
        if bbox is not None:
            if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                    or not all(isinstance(x, (int, float)) and 0 <= x <= 1 for x in bbox)
                    or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
                raise ToolError("bbox 写成 [x0, y0, x1, y1]：相对整页宽高，取值 0~1，原点左上角，x0<x1、y0<y1。"
                                "引用、事实的 bbox 可以直接拿来用。")
        with self.proj.connect() as con:
            if not con.execute("SELECT 1 FROM pages WHERE page_id=?", (page_id,)).fetchone():
                raise ToolError(f"没有页面 {page_id}。")
        st = self._state()
        path, idx, count, unsafe = export._page_source(self.proj, page_id)
        owner = navigate.page_owner(st, page_id) or {}
        out = {"page_id": page_id, **owner, "page_number": idx, "page_count": count,
               "workbench_pdf": f"/api/page/{page_id}.pdf"}
        if owner.get("version_state") not in (None, "现行"):
            out["warning"] = (f"这一页是 {owner['sheet']} 的 {owner['rev'] or '没写版次'} 版（{owner['version_state']}），"
                              f"现行是 {owner['current_rev'] or '—'} 版（page_id {owner['current_page_id'] or '—'}），建模用现行版。")
        if unsafe:
            out["file_note"] = "原文件带脚本等风险内容，不给原文件路径；请在工作台里打开（会去掉脚本），或用导出的现行图纸。"
        else:
            out["file"] = str(path)
        with pymupdf.open(path) as doc:
            page = doc[idx - 1]
            w, h = page.rect.width, page.rect.height
            out["page_size_pt"] = [round(w, 1), round(h, 1)]
            on_page = navigate.page_relations(st, page_id)
            if bbox is not None:
                pad = 0.01
                box = [max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(1, bbox[2] + pad), min(1, bbox[3] + pad)]
                rect = pymupdf.Rect(box[0] * w, box[1] * h, box[2] * w, box[3] * h)
                text = page.get_text("text", clip=rect).strip()
                out["region"] = {"bbox": list(bbox), "bbox_pt": [round(v, 1) for v in rect],
                                 "text": text[:2000] + ("……（已截断）" if len(text) > 2000 else "") if text else None,
                                 "references_here": [r for r in on_page if navigate.overlaps(r["bbox"], box)]}
                if not text:
                    out["region"]["note"] = "这块地方没有文字层（扫描件、图块或只有线条），要看图形请打开原图。"
        out["references_on_page"] = on_page
        out["hint"] = ("file 是本机文件，page_number 从 1 数；bbox_pt 是 PDF 坐标（点，原点左上角）。"
                       "这一页的识别内容用 get_page(page_id) 看。")
        return out

    def cad_file(self, no: str) -> dict:
        """一张图的 CAD 文件转成 DXF，给 Revit MCP 的 read_cad、cad_to_model、compare_cad_with_model 用。"""
        from . import cadfile
        st = self._state()
        s = self._sheet(st, no)
        try:
            out = cadfile.for_sheet(self.proj, st, s)
        except cadfile.CadError as e:
            raise ToolError(str(e)) from None
        tips = ["dxf 是本机文件路径，交给 Revit MCP 的 read_cad 先看图层、和用户确认，再 cad_to_model 翻模，建完用 compare_cad_with_model 核对。"]
        if out.get("region"):
            tips.append("三个工具都带上 region（这张图在模型空间里的范围），不然同一个文件里的其他图也会被读进去。")
        elif out.get("region_note"):
            tips.append("没有 region：照 region_note 说的办，别把整个模型空间当成这张图。")
        if out.get("dxf_warning"):
            tips.append("先处理 dxf_warning：丢了块的 DXF 翻出来缺轴号、轴线。")
        if out.get("xrefs"):
            tips.append("xrefs 是外部参照（轴网、底图常在里面），要一起叠上。")
        tips.append("构件依据写这张图的图号、版次和 page_id：cad_to_model 的 basis 填 {sheet, rev, page_id}。")
        out["hint"] = "".join(tips)
        return out

    def check_basis(self, items: list[dict]) -> dict:
        """建模依据核对：模型里每个构件写了依据哪张图哪一版、哪一页哪块地方、读到的数值，逐条对照现在的图纸。

        items 每条：element（构件名，必填）、sheet（图号，必填）、rev（用的版次）、page_id、bbox、value（从图上读的数值或文字）、
        notices_applied（已经按它改过的变更单编号）。结果每条一个 status：对得上 / 要复核 / 不合格，reasons 写为什么。
        只读：清单由建模的 agent 自己保存在它的工作文件夹里，图纸升版后再核一遍就知道哪些构件受影响。
        """
        if not isinstance(items, list) or not items:
            raise ToolError("items 是构件清单，每条至少有 element 和 sheet。")
        if len(items) > MAX_BASIS:
            raise ToolError(f"一次最多核对 {MAX_BASIS} 条，请分几次。")
        st = self._state()
        rows, count = [], {"对得上": 0, "要复核": 0, "不合格": 0}
        for it in items:
            it = it if isinstance(it, dict) else {}
            fail, review, ok = [], [], []
            element = str(it.get("element") or "").strip()
            if not element:
                fail.append("没写是哪个构件（element）")
            sheet = None
            if not str(it.get("sheet") or "").strip():
                fail.append("没写依据哪张图（sheet）")
            else:
                try:
                    sheet = self._sheet(st, str(it["sheet"]))
                except ToolError as e:
                    fail.append(str(e).split("。")[0] + "：依据的图不在台账里（缺图或图号写错）")
            if sheet:
                if sheet["status"] == "guess":
                    review.append("这张图还没看图识别，图号、版次是按文件名推测的")
                elif sheet["status"] in ("voided", "missing"):
                    fail.append(f"这张图{STATUS_LABEL[sheet['status']]}，不能当依据")
                elif sheet["status"] in ("conflict", "pending_void"):
                    review.append(f"这张图的状态是“{STATUS_LABEL[sheet['status']]}”，先在问题中心定下来")
                rev = it.get("rev")
                cur = str(sheet["current_rev"] or "").strip()
                cur = "" if cur == "?" else cur  # 图上没写版次
                if not cur and not str(rev or "").strip():
                    ok.append("这张图本身没有版次（修改记录是空的），依据也没写版次")
                elif rev is not None and str(rev).strip() != cur:
                    review.append(f"用的是 {rev or '没写版次'} 版，现行是 {cur or '没写版次'} 版")
                elif rev is None:
                    review.append("没写用的是哪一版（rev）")
                pid = it.get("page_id")
                if pid:
                    owner = navigate.page_owner(st, str(pid))
                    if not owner:
                        fail.append(f"没有页面 {pid}")
                    elif owner.get("sheet") != sheet["no"]:
                        fail.append(f"页面 {pid} 不是 {sheet['no']}，是 {owner.get('sheet') or owner.get('notice')}")
                    elif owner["version_state"] != "现行":
                        review.append(f"页面 {pid} 是{owner['version_state']}的 {owner['rev'] or '没写版次'} 版，"
                                      f"现行版在 {sheet['current_page'] or '—'}")
                else:
                    review.append("没写在哪一页（page_id），检查时没法回到原图")
                done = {str(x) for x in it.get("notices_applied") or ()}
                for n in st["notices"]:
                    if sheet["no"] in n["affects"] and n["id"] not in done:
                        review.append(f"变更单 {n['id']}（{n['title']}）改过这张图，确认构件已按它改，"
                                      f"改过的话写进 notices_applied")
                for i in st["issues"]:
                    if (i["level"] in ("crit", "warn") and not i.get("confirmed") and i["kind"] not in BASIS_SKIP
                            and (i["target"].get("sheet") == sheet["no"] or sheet["no"] in i.get("sheets", []))):
                        review.append(f"这张图有未处理的问题：{i['title']}")
                value = str(it.get("value") or "").strip()
                if value and pid and not fail:
                    review_or_ok = self._value_on_page(str(pid), it.get("bbox"), value)
                    (ok if review_or_ok[0] else review).append(review_or_ok[1])
            status = "不合格" if fail else ("要复核" if review else "对得上")
            count[status] += 1
            rows.append({"element": element or None, "sheet": sheet["no"] if sheet else it.get("sheet"),
                         "status": status, "reasons": fail + review, **({"checked": ok} if ok else {})})
        return {"summary": count, "items": rows,
                "hint": "“不合格”的构件没有可靠依据，要改；“要复核”的逐条看 reasons，用 locate(page_id, bbox) 回到原图核对。"
                        "图纸升版、收到变更单后，把整份清单再核一遍，就知道哪些构件受影响。"}

    def _value_on_page(self, page_id: str, bbox, value: str) -> tuple[bool, str]:
        """数值在不在原图那块地方（没有 bbox 或没有文字层时，看这一页的识别内容）。"""
        squash = lambda t: re.sub(r"[x×X*＊]", "x", re.sub(r"\s+", "", t or ""))  # noqa: E731  # 350×850 = 350x850
        try:
            region = self.locate(page_id, bbox).get("region") if bbox is not None else None
        except ToolError as e:
            return False, f"bbox 用不了：{e}"
        if region and region.get("text"):
            if squash(value) in squash(region["text"]):
                return True, f"原图这块地方有“{value}”"
            return False, f"原图这块地方没找到“{value}”，读到的是：{region['text'][:80]}"
        detail = self.get_page(page_id).get("detail") or ""
        where = "这块地方没有文字层，" if region else ""
        if len(squash(value)) <= 3:  # 轴号、短编号（A、3、H1）：随便一段字里都有，要整行就是它才算（轴号圈、索引符号的属性单独一行）
            with self.proj.connect() as con:
                row = con.execute("SELECT text FROM pages WHERE page_id=?", (page_id,)).fetchone()
            text = (row["text"] if row else "") or ""
            lines = {squash(x).strip("|*-:：") for x in (detail + "\n" + text).splitlines()}
            if squash(value) in lines:
                return True, f"{where}这一页有单独写着“{value}”的字（轴号、编号；位置要对照原图）"
            return False, f"{where}这一页找不到单独写着“{value}”的字（轴号、编号），要打开原图看"
        if squash(value) in squash(detail):
            return True, f"{where}这一页的识别内容里有“{value}”（位置要对照原图）"
        with self.proj.connect() as con:  # DWG、有文字层的 PDF：页面上的字（识别内容只摘了一部分）
            row = con.execute("SELECT text FROM pages WHERE page_id=?", (page_id,)).fetchone()
        if row and squash(value) in squash(row["text"]):
            return True, f"{where}这一页的文字里有“{value}”（位置要对照原图）"
        return False, f"{where}这一页的识别内容和文字里都没有“{value}”，要打开原图看"

    # ---------------- 查询：事实、问题 ----------------

    def list_facts(self, group: str | None = None, only_problems: bool = False) -> dict:
        st = self._state()
        rows = []
        for f in self._facts(st):
            if group and f["group"] != group:
                continue
            if only_problems and f["conf"] not in ("conflict", "low"):
                continue
            rows.append({"key": f["key"], "group": f["group"], "name": f["name"], "value": f["value"],
                         "confidence": CONF_LABEL[f["conf"]], "sources": f["sources"], "note": f["note"]})
        return {"groups": [g["label"] for g in st["facts"]["groups"]], "facts": rows}

    def get_fact(self, name: str) -> dict:
        st = self._state()
        facts = self._facts(st)
        key = state.norm_fact_key(name)
        f = next((x for x in facts if x["key"] == key), None) or next(
            (x for x in facts if name in x["name"] or key in x["key"]), None)
        if not f:
            raise ToolError(f"没有事实「{name}」。可以先用 list_facts 查看全部事实。")
        return {"key": f["key"], "group": f["group"], "name": f["name"], "value": f["value"],
                "confidence": CONF_LABEL[f["conf"]], "note": f["note"],
                "evidence": [{"sheet": e["sheet"], "rev": e["rev"], "page_id": e["page_id"], "value": e["value"],
                              "text": e["text"], "source": e["source"], "current": e["active"]} for e in f["evidence"]]}

    def floor_coverage(self) -> list[dict]:
        st = self._state()
        rows = [{"group": r["group"], "result": reports.FLOOR_STATUS[r["status"]], "basis": r["basis"],
                 "covered": r["covered"], "missing": r["missing_text"] or None, "remarks": r.get("remarks", [])}
                for r in st["floor_coverage"]]
        return rows or [{"group": None, "result": "还没法核对", "covered": None, "missing": None, "remarks": [],
                         "basis": "要知道地上、地下各几层（设计说明、剖面），和平面图各是哪一层（图名），看图识别以后才有；"
                                  "只读了 DWG 图签、或图名里没写楼层的，先识别设计说明、剖面和平面图"}]

    def list_issues(self, level: str | None = None, kind: str | None = None) -> dict:
        st = self._state()
        lv = {"严重": "crit", "提醒": "warn", "信息": "info"}.get(level, level) if level else None
        rows = []
        for i in st["issues"]:
            if lv and i["level"] != lv:
                continue
            if kind and i["kind"] != kind:
                continue
            rows.append({"id": i["id"], "level": {"crit": "严重", "warn": "提醒", "info": "信息"}[i["level"]],
                         "kind": i["kind"], "title": i["title"], "detail": i["detail"],
                         "options": [] if i["kind"] == "风险文件" else [o["label"] for o in i["options"]],
                         "confirmed": i.get("confirmed", False)})
        return {"total": len(rows), "issues": rows, "how_to_choose": guide.help_for(rows),
                "hint": "how_to_choose 按问题编号冒号前那段写着：是什么、怎么看、拿不准怎么选、选了以后，和工作台里的“怎么选？”是同一份；"
                        "用户问怎么选时照它说。要处理某个问题，用 propose_issue_resolution(issue_id, option) 提议，等用户确认。"}

    def draft_qa(self) -> dict:
        return {"draft": reports.qa_report(self._state()),
                "note": "这是根据缺图和冲突自动整理的草稿；只收录高可靠缺图和已确认要图的事项，提交前需要人工核对。"}

    def list_baselines(self) -> list[dict]:
        return self._state()["baselines"]

    def estimate_recognition(self, scope: str = "new", tier: str = "economy", executor: str = "codex") -> dict:
        """估算一次真实识别的规模、用时或花费，按分步识别（地图 → 重点图 → 建模主图 → 按需深入）分好。不生成任务包、不调用 API。"""
        from . import ops
        if scope not in recognition.SCOPES:
            raise ToolError(f"scope 可选：{'、'.join(recognition.SCOPES)}")
        if tier not in ("economy", "strong"):
            raise ToolError("tier 可选：economy、strong")
        if executor not in ("codex", "claude"):
            raise ToolError("executor 可选：codex、claude")
        est = ops.plan_recognition(self.proj, scope, executor, tier)
        if est is None:
            return {"pages": 0, "note": f"范围「{recognition.SCOPES[scope]}」内没有需要识别的页面。"}
        steps = [{"step": i, "key": st["key"], "name": st["label"], "what": st["desc"], "pages_total": st["total"],
                  "recognized": st["done"], "pages_left": st["left"],
                  **({"minutes": st["minutes"]} if "minutes" in st else {"estimated_usd": st.get("usd")} if st["left"] else {}),
                  "groups": [{"name": g["label"], "pages": g["pages"], "examples": g["examples"]} for g in st["groups"]]}
                 for i, st in enumerate(est["stages"], 2)]
        where = "OpenAI（Codex，占用 ChatGPT 账号的额度）" if executor == "codex" else "Anthropic API，并产生费用"
        return {"pages": est["pages"], "executor": executor, "model": est["model"],
                **({"minutes": est["minutes"]} if executor == "codex" else {"estimated_usd": est["estimated_usd"]}),
                "map": {"step": 1, "what": "地图：只看文件名、文件夹、PDF 文字和 DWG，不看图、不花钱", **est["map"]},
                "stages": steps,
                "command": f"bidbench recognize --executor {executor} --tier {tier} --scope {scope} --stage key",
                "note": "分步识别：第 1 步地图已经有了（收图时就做了）；建议先识别第 2 步重点图（目录、变更单、清单和表、说明头两页），"
                        "再第 3 步建模主图（平面、立面、剖面）；第 4 步按需深入不用全识别，建模用到哪张再识别哪张。"
                        f"每一步可以随时停，下次接着识别没做完的。识别会把图纸页面发送到 {where}；请让用户自己决定是否开始、识别哪几步。"}

    def numbering_overview(self) -> dict:
        st = self._state()
        items = state.numbering_items(self.proj)
        nb = st.get("numbering") or {}
        cur = {k: nb[k] for k in ("status", "source", "text", "format", "coverage") if nb.get(k) is not None}
        return {**numbering.overview(items), "folders": numbering.folder_summary(items),
                "catalog_pages": numbering.catalog_pages(items), "current": cur or "还没有图号格式",
                "roles": numbering.ROLES, "disciplines": classify.DISCIPLINES, "type_kinds": numbering.TYPE_KINDS,
                "how_to": "每组是段数相同的图号：position 是第几段，values 里是这一段出现过的代码、文件数、例图名、一个页面编号。"
                          "看不懂的代码按这个顺序找：先看例图名；再用 get_page(page_id, include_text_layer=true) 看 catalog_pages "
                          "里的目录页和这个代码的例图的文字；还不懂，把要看图识别的页面编号写进 look_pages（不要自己开始识别）；"
                          "都找不到就写进 unsure。最后用 propose_numbering 提议，why 里写清每个代码是从哪看出来的。"}

    def list_notes(self) -> list[dict]:
        with self.proj.connect() as con:
            return [dict(r) for r in con.execute("SELECT id, topic, text, source, created_at FROM notes ORDER BY id")]

    def list_proposals(self, status: str = "pending") -> list[dict]:
        with self.proj.connect() as con:
            q = "SELECT * FROM proposals" + ("" if status == "all" else " WHERE status=?") + " ORDER BY id"
            rows = con.execute(q, () if status == "all" else (status,)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "status": r["status"], "summary": _summary(r["kind"], json.loads(r["payload"])),
                 "reason": r["reason"], "created_at": r["created_at"], "outcome": r["outcome"]} for r in rows]

    # ---------------- 提议（只写提议，不生效） ----------------

    def _propose(self, kind: str, payload: dict, reason: str) -> dict:
        if not reason or not reason.strip():
            raise ToolError("提议必须写明理由（reason），并尽量引用图号、版次或页面编号。")
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.proj.connect() as con:
            dup = con.execute("SELECT id FROM proposals WHERE status='pending' AND kind=? AND payload=?", (kind, body)).fetchone()
            if dup:
                return {"proposal_id": dup["id"], "status": "已有相同的待确认提议", "summary": _summary(kind, payload)}
            cur = con.execute("INSERT INTO proposals(kind, payload, reason, status, source, created_at) VALUES (?,?,?,?,?,?)",
                              (kind, body, reason.strip(), "pending", self.source, now_iso()))
            pid = cur.lastrowid
        return {"proposal_id": pid, "status": "待用户确认", "summary": _summary(kind, payload),
                "how_to_confirm": f"请用户在工作台“AI 助手提议”里确认，或运行 bidbench confirm {pid}。确认前不会生效。"}

    def propose_issue_resolution(self, issue_id: str, option: str, reason: str) -> dict:
        st = self._state()
        issue = next((i for i in st["issues"] if i["id"] == issue_id), None)
        if not issue:
            raise ToolError(f"没有问题 {issue_id}。用 list_issues 查看问题编号。")
        if issue["kind"] == "风险文件":
            raise ToolError("文件安全问题只能由用户自己判断，助手不提议。可以提醒用户在工作台问题中心处理。")
        if not issue["options"]:
            raise ToolError("这个问题没有可选的处理方式，需要用户自己处理。")
        opts = issue["options"]
        pick = None
        if str(option).isdigit() and 1 <= int(option) <= len(opts):
            pick = opts[int(option) - 1]
        else:
            pick = next((o for o in opts if o["label"] == option), None)
        if not pick:
            raise ToolError("option 应为下列之一（序号或原文）：" + "；".join(f"{n}. {o['label']}" for n, o in enumerate(opts, 1)))
        return self._propose("decision", {"issue_id": issue_id, "issue_title": issue["title"], "label": pick["label"],
                                          "kind": pick["kind"], "key": pick["key"], "value": pick["value"]}, reason)

    def propose_fact_confirmation(self, fact: str, value: str, reason: str) -> dict:
        f = self.get_fact(fact)
        if not value or not value.strip():
            raise ToolError("请给出要确认的值，例如“20 层”。")
        return self._propose("decision", {"issue_id": f"fact:{f['key']}", "issue_title": f"确认事实 {f['name']}",
                                          "label": f"{f['name']} = {value.strip()}", "kind": "fact",
                                          "key": f["key"], "value": value.strip()}, reason)

    def propose_baseline(self, name: str, reason: str, sheets: list[str] | None = None) -> dict:
        if not name or not name.strip():
            raise ToolError("请给出基线名称，例如“3 层结构模型”。")
        st = self._state()
        nos = [self._sheet(st, s)["no"] for s in sheets] if sheets else None
        return self._propose("baseline", {"name": name.strip(), "sheets": nos}, reason)

    def propose_improvement(self, title: str, problem: str, suggestion: str) -> dict:
        if not (title or "").strip() or not (suggestion or "").strip():
            raise ToolError("请写清楚标题（title）和建议（suggestion）。")
        if not (problem or "").strip():
            raise ToolError("请写清楚遇到了什么问题（problem）。")
        return self._propose("improvement", {"title": title.strip(), "suggestion": suggestion.strip()}, problem)

    def propose_numbering(self, format: dict, reason: str) -> dict:  # noqa: A002 - 参数名就是 AI 看到的名字
        try:
            fmt = numbering.validate(format)
        except numbering.FormatError as e:
            raise ToolError(str(e)) from None
        nb = numbering.Numbering(fmt, "proposed")
        cov = nb.coverage(state.numbering_items(self.proj))
        if not cov["matched"]:
            raise ToolError("这个格式一个文件的图号都套不上，请对照 numbering_overview 的分组再看看。")
        return {**self._propose("numbering", {"format": fmt, "text": numbering.describe(fmt),
                                              "coverage": {k: cov[k] for k in ("total", "matched", "unmatched")}}, reason),
                "coverage": cov}

    def propose_note(self, topic: str, text: str, reason: str) -> dict:
        if not topic or not text:
            raise ToolError("笔记需要主题和内容。")
        return self._propose("note", {"topic": topic.strip(), "text": text.strip()}, reason)

    # ---------------- 确认 / 驳回（只给人用：工作台、命令行） ----------------

    def confirm(self, proposal_id: int) -> dict:
        with self.proj.connect() as con:
            r = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not r:
            raise ToolError(f"没有提议 #{proposal_id}")
        if r["status"] != "pending":
            raise ToolError(f"提议 #{proposal_id} 已经处理过（{r['status']}）")
        p = json.loads(r["payload"])
        outcome = ""
        try:
            if r["kind"] == "decision":
                if p["kind"] != "fact":
                    issue = next((i for i in self._state()["issues"] if i["id"] == p["issue_id"]), None)
                    still = issue and any(o["kind"] == p["kind"] and o["key"] == p["key"] and o["value"] == p["value"]
                                          for o in issue["options"])
                    if not still:
                        return self._close(proposal_id, "stale", "情况已经变化（问题已解决或选项不同），提议失效，没有执行。")
                add_decision(self.proj, p["kind"], p["key"], p["value"], r["reason"], actor=CONFIRMED_ACTOR)
                outcome = f"已记录决定：{p['label']}"
            elif r["kind"] == "baseline":
                snap = state.create_baseline(self.proj, p["name"], p["sheets"], r["reason"])
                outcome = f"已锁定基线「{p['name']}」：{len(snap)} 张图"
            elif r["kind"] == "improvement":
                add_suggestion(p["title"], r["reason"], p["suggestion"])
                outcome = f"已记入改进建议清单「{p['title']}」，开发时处理"
            elif r["kind"] == "numbering":
                value = numbering.decision_value(json.dumps(p["format"], ensure_ascii=False))
                add_decision(self.proj, "numbering", "*", value, r["reason"], actor=CONFIRMED_ACTOR)
                outcome = f"已确认图号格式：{p['text']}"
            elif r["kind"] == "note":
                with self.proj.connect() as con:
                    con.execute("INSERT INTO notes(topic, text, source, created_at) VALUES (?,?,?,?)",
                                (p["topic"], p["text"], CONFIRMED_ACTOR, now_iso()))
                outcome = f"已保存项目笔记「{p['topic']}」"
            else:
                raise ToolError(f"未知的提议类型 {r['kind']}")
        except ValueError as e:
            return self._close(proposal_id, "stale", f"无法执行：{e}")
        return self._close(proposal_id, "confirmed", outcome)

    def reject(self, proposal_id: int, note: str | None = None) -> dict:
        with self.proj.connect() as con:
            r = con.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not r:
            raise ToolError(f"没有提议 #{proposal_id}")
        if r["status"] != "pending":
            raise ToolError(f"提议 #{proposal_id} 已经处理过（{r['status']}）")
        return self._close(proposal_id, "rejected", note or "用户驳回")

    def _close(self, pid: int, status: str, outcome: str) -> dict:
        with self.proj.connect() as con:
            con.execute("UPDATE proposals SET status=?, decided_at=?, outcome=? WHERE id=?", (status, now_iso(), outcome, pid))
        self._cache.clear()
        return {"proposal_id": pid, "status": status, "outcome": outcome}


def _summary(kind: str, p: dict) -> str:
    if kind == "decision":
        return f"{p['issue_title']} → {p['label']}"
    if kind == "baseline":
        return f"锁定基线「{p['name']}」（{'、'.join(p['sheets']) if p['sheets'] else '全部现行图纸'}）"
    if kind == "note":
        return f"项目笔记「{p['topic']}」：{p['text']}"
    if kind == "improvement":
        return f"改进建议「{p['title']}」：{p['suggestion']}"
    if kind == "numbering":
        c = p.get("coverage") or {}
        return f"图号格式：{p['text']}（套得上 {c.get('matched', '?')}/{c.get('total', '?')} 个文件）"
    return kind


# ---------------- 改进建议清单（本机，给开发者） ----------------

def suggestions_path() -> Path:
    return appconfig.config_dir() / SUGGESTIONS


def add_suggestion(title: str, problem: str, suggestion: str) -> None:
    with open(suggestions_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now_iso(), "title": title, "problem": problem, "suggestion": suggestion},
                           ensure_ascii=False) + "\n")


def list_suggestions() -> list[dict]:
    p = suggestions_path()
    rows = []
    for ln in (p.read_text(encoding="utf-8").splitlines() if p.exists() else []):
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("title"):
            rows.append(r)
    return rows


def open_toolkit(project_dir: str | Path, source: str = AI_SOURCE) -> Toolkit:
    return Toolkit(Project.open(Path(project_dir)), source)
