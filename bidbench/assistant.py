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

from . import appconfig, recognition, reports, state
from .recognition import page_markdown
from .state import CONF_LABEL, STATUS_LABEL
from .store import Project, add_decision, now_iso

AI_SOURCE = "AI 助手"
SUGGESTIONS = "改进建议.jsonl"  # 在用户目录（appconfig.config_dir()），不在项目里：建议针对的是软件，不是项目
CONFIRMED_ACTOR = "AI 助手建议，我确认"
MAX_LIST = 200


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
        n = state.norm_no(no)
        for s in st["sheets"]:
            if s["no"] == n:
                return s
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
            "hint": "逐层往下查：list_sheets / get_sheet 看图纸，list_facts / get_fact 看事实，list_issues 看问题。",
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
                         "page_id": s["current_page"]})
        limit = max(1, min(limit, MAX_LIST))
        return {"total": len(rows), "sheets": rows[:limit], "truncated": len(rows) > limit,
                "statuses": list(STATUS_LABEL.values())}

    def get_sheet(self, no: str) -> dict:
        st = self._state()
        s = self._sheet(st, no)
        refs = [{"direction": "引用" if e["from"] == s["no"] else "被引用",
                 "sheet": e["to"] if e["from"] == s["no"] else e["from"], "text": e["label"]}
                for e in st["edges"] if s["no"] in (e["from"], e["to"])]
        facts = []
        for f in self._facts(st):
            if s["no"] not in f["sources"]:
                continue
            own = next((e for e in f["evidence"] if e["active"] and e["sheet"] == s["no"]), None)
            facts.append({"key": f["key"], "name": f["name"], "this_sheet": (own["text"] or own["value"]) if own else None,
                          "project_value": f["value"], "confidence": CONF_LABEL[f["conf"]]})
        issues = [{"id": i["id"], "kind": i["kind"], "title": i["title"], "detail": i["detail"],
                   "relation": "本图" if i["target"].get("sheet") == s["no"] else "相关"}
                  for i in st["issues"] if i["target"].get("sheet") == s["no"] or s["no"] in i.get("sheets", [])]
        return {"no": s["no"], "title": s["title"], "discipline": s["discipline"], "status": STATUS_LABEL[s["status"]],
                "current_rev": s["current_rev"], "current_page_id": s["current_page"], "paths": s["paths"],
                "notes": s["notes"],
                "versions": [{"rev": v["rev"], "date": v["date"], "batch": v["batch"], "state": v["state"],
                              "page_id": v["page_id"]} for v in s["versions"]],
                "references": refs, "facts": facts, "issues": issues}

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
        return [{"group": r["group"], "result": reports.FLOOR_STATUS[r["status"]], "basis": r["basis"],
                 "covered": r["covered"], "missing": r["missing_text"] or None, "remarks": r.get("remarks", [])}
                for r in st["floor_coverage"]]

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
        return {"total": len(rows), "issues": rows,
                "hint": "要处理某个问题，用 propose_issue_resolution(issue_id, option) 提议，等用户确认。"}

    def draft_qa(self) -> dict:
        return {"draft": reports.qa_report(self._state()),
                "note": "这是根据缺图和冲突自动整理的草稿；只收录高可靠缺图和已确认要图的事项，提交前需要人工核对。"}

    def list_baselines(self) -> list[dict]:
        return self._state()["baselines"]

    def estimate_recognition(self, scope: str = "new", tier: str = "economy") -> dict:
        """估算一次真实识别的规模和花费，不生成任务包、不调用 API。"""
        from .executors import claude
        if scope not in recognition.SCOPES:
            raise ToolError(f"scope 可选：{'、'.join(recognition.SCOPES)}")
        if tier not in claude.TIERS:
            raise ToolError("tier 可选：economy、strong")
        only = recognition._conflict_pages(self.proj) if scope == "conflict" else None
        with self.proj.connect() as con:
            rows = recognition.pages_to_recognize(con, scope=scope, only=only)
        cfg = claude.TIERS[tier]
        tiles = cfg["tiles"]
        per_image = claude.MAX_IMAGE_EDGE ** 2 * 0.7 / 750
        images = len(rows) * (2 + (tiles * tiles if tiles > 1 else 0))
        chars = sum(min(len(r["text"]) * 1.6, claude.MAX_TEXT_CHARS) for r in rows)
        tin = images * per_image + chars / 1.5 + len(rows) * 1500
        tout = len(rows) * 2500
        pin, pout = claude.PRICES[cfg["model"]]
        return {"pages": len(rows), "model": cfg["model"], "estimated_usd": round((tin * pin + tout * pout) / 1e6, 4),
                "command": f"bidbench recognize --executor claude --tier {tier} --scope {scope}",
                "note": "识别会把图纸页面发送到 Anthropic API，并产生费用；请让用户自己决定是否运行上面的命令。"}

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
