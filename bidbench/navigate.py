"""给 AI 建模找图用的导航：按任务找图、顺着关系往外走、跳到页面上的某个位置。

只读派生状态（state.build 的结果），不改关系怎么算。现有的几种关系在这里统一写成一种格式
（种类、方向、原文、出处页面、位置）：
- 引用、跨专业引用、目录列出：来自 st["edges"]（看图识别的引用、目录条目）
- 变更单：来自 st["notices"] 的 affects（变更单 → 它改了的图）
- 同层、通用图：来自 state.same_floor()（按楼层临时算，没有出处位置）
引用分种类（form：详图索引、剖切、文字引用、变更涉及、目录列出，docs/图纸字段.md 9.6），带编号（mark）；
被指的图上读出了同编号的详图时带 target（那块详图的页面和位置），AI 可以用 locate 直接跳过去。
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import floors, state
from .state import STATUS_LABEL

REL_LABEL = {"ref": "引用", "cross": "跨专业引用", "catalog": "目录列出", "notice": "变更单",
             "same_floor": "同层", "general": "通用图", "xref": "共用参照"}
REL_NOTE = {
    "引用": "图上写了“详见”“见”或详图索引，指向同专业的图（from 写了，to 被引用）",
    "跨专业引用": "同上，指向别的专业的图",
    "目录列出": "图纸目录列了这张图、手上却没有（缺图）",
    "变更单": "变更单、答疑改了这张图的局部（from 是变更单编号，text 是那一条的原文，mark 是改的哪个详图），相关内容以变更单为准",
    "同层": "两张图管同一栋楼的同一层（按图名、适用楼层算），没有画在图上的对应关系",
    "通用图": "设计说明、没写楼层的柱平面等，建哪一层都要看",
    "共用参照": "DWG 里用了同一个外部参照（同一个核心筒、同一套柱子），text 是参照名",
}
EXPLICIT = ("引用", "跨专业引用", "目录列出", "变更单")
FORM_NOTE = {
    "详图索引": "圆圈详图索引符号（⑤/建施-12），mark 是详图编号；有 target 时就是被指那张图上的那块详图",
    "剖切": "剖切符号（1-1 剖面见建施-05、301/1），mark 是剖面编号",
    "立面": "立面索引符号（201/1），mark 是立面编号",
    "文字引用": "“详见”“见”某张图，指整张图",
    "变更涉及": "变更单条文写到的图，mark 是改的哪个详图",
    "目录列出": "图纸目录的一行",
}
FLOOR_BASED = ("同层", "通用图", "共用参照")
MAX_DEPTH = 3
MAX_NODES = 120
ROLE_ORDER = ["主图", "按图名找到", "通用图", "引用到的图", "引用它的图", "同层其他图"]


def relations(st: dict) -> list[dict]:
    """全部有出处的关系（引用、跨专业引用、目录列出、变更单），一条一行，同一对图在不同位置写了几次就有几行。"""
    by_no = {s["no"]: s for s in st["sheets"]}
    out = [{"type": REL_LABEL.get(e["type"], e["type"]), "form": e.get("form") or "文字引用", "from": e["from"], "to": e["to"],
            "text": e["label"], "page_id": e["page_id"], "bbox": e["bbox"], "mark": e.get("mark"), "target": e.get("target")}
           for e in st["edges"]]
    for n in st["notices"]:
        refs = n.get("refs") or [{"to": no, "text": n["title"] or n["kind"], "mark": None, "bbox": None} for no in n["affects"]]
        out += [{"type": "变更单", "form": "变更涉及", "from": n["id"], "to": r["to"], "text": r["text"],
                 "page_id": n["page_id"], "bbox": r["bbox"], "mark": r["mark"],
                 "target": state._detail_at(by_no.get(r["to"]), r["mark"])} for r in refs]
    return out


def sheet_row(s: dict) -> dict:
    """一张图在导航结果里的写法：够 AI 决定要不要打开，打开用 page_id（get_page、locate）。"""
    files = {k: v for k, v in (("pdf", s["paths"][:3]), ("cad", (s.get("cad") or [])[:3])) if v}
    cover = coverage_text(s)
    return {"no": s["no"], "title": s["title"], "discipline": s["discipline"], "status": STATUS_LABEL[s["status"]],
            "current_rev": s["current_rev"], "page_id": s["current_page"], "floors": s.get("floors_name") or s.get("floors_text"),
            **({"tower": s["tower"]} if s.get("tower") else {}), **({"covers": cover} if cover else {}),
            **({"depth": state.DEPTHS[s["depth"]][0]} if s.get("depth") else {}),
            **({"details": [f"{d['mark']} {d['title'] or ''}".strip() for d in s["details"]]} if s.get("details") else {}),
            **({"files": files} if files else {}),
            **({"notes": s["notes"]} if s["notes"] else {})}


def coverage_text(s: dict) -> str | None:
    """分区和轴线范围，如“A区 1~12/A~F”。读不到为 None。"""
    f = s.get("fields") or {}
    v = lambda k: (f.get(k) or {}).get("value")  # noqa: E731
    axes = "/".join(x for x in (v("axis_x"), v("axis_y")) if x)
    return " ".join(x for x in (v("zone"), axes and f"{axes} 轴") if x) or None


def parse_floor(st: dict, text: str) -> set[int]:
    """任务里的楼层：“3”“3层”“3F”“三层”“B1”“地下一层”“屋面”“3~5层”，英文的“Level 03”按项目里图名的数法。"""
    t = (text or "").strip()
    if re.fullmatch(r"-?\d{1,3}", t):
        n = int(t)
        return {n} if n > 0 else {n} if n < 0 else set()
    if re.search(r"[A-Za-z]{2}", t):  # 英文叫法：先找项目里写法一样的图，用它换算的结果（Level 01 是首层还是 2F，项目自己定）
        low = t.lower()
        for s in st["sheets"]:
            raw = (s.get("floors_name") or "").split("（")[0]
            if len(s.get("floors") or ()) == 1 and raw.lower() == low:
                return set(s["floors"])
    return floors.parse_floors(t)


def _words(keyword: str | None) -> list[str]:
    return [w for w in re.split(r"[\s,，、/;；]+", keyword or "") if w]


def _disc_ok(s: dict, discipline: str | None) -> bool:
    return not discipline or discipline in s["discipline"] or s["discipline"] in discipline


def _content(s: dict) -> str:
    c = (s.get("fields") or {}).get("content")
    return (c or {}).get("value") or ""


def find_for_task(st: dict, floor: str | None = None, discipline: str | None = None, keyword: str | None = None,
                  tower: str | None = None, limit: int = 40) -> dict:
    """按任务找图：楼层 + 专业 + 构件或关键词。返回该看的现行图、通用图、图上引用到的图，以及改过它们的变更单和相关问题。

    主图：楼层对得上、专业对得上、图名或内容里有关键词（没给关键词时楼层、专业对得上就算）。
    按图名找到：没写楼层、图名里有关键词的图（门窗表、梁柱节点详图）。通用图：专业对得上的设计说明、没写楼层的柱平面。
    引用到的图：主图上写了“详见”的图，往外走一步；没发来的标“缺失”。没给楼层时，再列出写了“详见”主图的图（引用它的图）。
    同层其他图：楼层、专业对得上、没有关键词。没给专业时，通用图只收主图那几个专业的。"""
    words = _words(keyword)
    if not (floor or discipline or words):
        raise ValueError("至少给出楼层、专业、关键词中的一样，例如 floor=“3F”、discipline=“结构”、keyword=“梁”。")
    want = parse_floor(st, floor) if floor else set()
    if floor and not want:
        raise ValueError(f"认不出楼层「{floor}」。可以写 3F、3层、B1、屋面，或项目图名里的写法（如 Level 03）。")
    tw = (tower or "").strip() or None
    if tw and st["towers"]["multi"] and tw not in {t["name"] for t in st["towers"]["list"]}:
        raise ValueError(f"没有塔楼「{tw}」。这个项目的塔楼：{'、'.join(t['name'] for t in st['towers']['list'])}。")

    picked: dict[str, dict] = {}
    pool: list[dict] = []
    for s in st["sheets"]:
        if s["status"] == "voided" or not _disc_ok(s, discipline):
            continue
        if tw and not state._same_building({"tower": tw}, s):
            continue
        fl = set(s.get("floors") or ())
        if want and fl and not (want & fl):
            continue
        title = s["title"] or ""
        in_title = [w for w in words if w in title or w in s["no"]]
        in_content = [w for w in words if w not in in_title and w in _content(s)]
        why = []
        if want and fl:
            why.append(f"楼层 {s['floors_text']}" + ("（正好是这几层）" if fl == want else "（含要找的层）"))
        if in_title:
            why.append(f"图名有“{'、'.join(in_title)}”")
        if in_content:
            why.append(f"内容摘要有“{'、'.join(in_content)}”")
        if want and fl:  # 这一层的平面图总是主图：墙、门窗、梁都画在平面上
            plan = "平面" in title and not in_title
            role = "主图" if (in_title or in_content or not words or plan) else "同层其他图"
            if plan and words:
                why.append("这一层的平面图")
        elif s.get("general"):
            if not (discipline or in_title or in_content):
                pool.append(s)  # 没指定专业时，等主图定了，只收主图那几个专业的通用图
                continue
            role, why = "通用图", why + ["设计说明、没写楼层的图，建哪一层都要看"]
        elif in_title or in_content:
            role = "主图" if not want else "按图名找到"
            if want:
                why.append("图名没写楼层")
        elif not want and not words:
            role = "主图"  # 只给了专业：这个专业的图都算
        else:
            continue
        picked[s["no"]] = dict(sheet_row(s), role=role, why=why)

    by_no = {s["no"]: s for s in st["sheets"]}
    sources = [no for no, r in picked.items() if r["role"] in ("主图", "按图名找到")]
    discs = {picked[no]["discipline"] for no in sources}
    for s in pool:
        if s["discipline"] in discs:
            picked[s["no"]] = dict(sheet_row(s), role="通用图",
                                   why=[f"主图里有{s['discipline']}的图；设计说明、没写楼层的图，建哪一层都要看"])
    # 没给楼层时，找到的多是详图、大样：也列出写了“详见”它的图，知道它用在哪
    details = {no for no in sources if not by_no[no].get("floors")} if not want else set()
    for rel in relations(st):
        if rel["type"] not in ("引用", "跨专业引用"):
            continue
        if rel["from"] in sources:
            other, role, note = rel["to"], "引用到的图", f"{rel['from']} 上写“{rel['text']}”"
        elif rel["to"] in details:
            other, role, note = rel["from"], "引用它的图", f"这张图上写“{rel['text']}”"
        else:
            continue
        r = picked.get(other)
        if r is None:
            t = by_no.get(other)
            if not t or t["status"] == "voided":
                continue
            r = picked[other] = dict(sheet_row(t), role=role, why=[])
        elif r["role"] == "同层其他图":
            r["role"] = role
        if note not in r["why"]:
            r["why"].append(note)
        if rel["target"] and role == "引用到的图":
            jump = dict(rel["target"], mark=rel["mark"])
            if jump not in r.setdefault("jump_to", []):
                r["jump_to"].append(jump)

    rows = sorted(picked.values(), key=lambda r: (ROLE_ORDER.index(r["role"]), state._disc_order(r["discipline"]),
                                                  state.no_parts(r["no"])[0], state.no_parts(r["no"])[1] or 0, r["no"]))
    nos = {r["no"] for r in rows if r["role"] != "同层其他图"}
    notices = [{"id": n["id"], "title": n["title"], "kind": n["kind"], "page_id": n["page_id"], "batch": n["batch"],
                "changes": [x for x in n["affects"] if x in nos]} for n in st["notices"] if nos & set(n["affects"])]
    first = {r["no"] for r in rows if r["role"] in ("主图", "按图名找到", "通用图")}
    issues = [{"id": i["id"], "kind": i["kind"], "title": i["title"], "detail": i["detail"]} for i in st["issues"]
              if i["level"] != "info" and (i["target"].get("sheet") in nos or first & set(i.get("sheets", [])))]
    limit = max(1, min(limit, 200))
    return {
        "query": {"floor": floors.compress(want) if want else None, "discipline": discipline, "keywords": words,
                  **({"tower": tw} if tw else {})},
        "total": len(rows), "sheets": rows[:limit], **({"truncated": len(rows) - limit} if len(rows) > limit else {}),
        "missing": [r["no"] for r in rows if r["status"] == "缺失"],
        "notices": notices, "issues": issues[:30],
        "roles": {"主图": "楼层、专业、关键词都对得上，先看", "按图名找到": "没写楼层、图名有关键词的图",
                  "通用图": "设计说明、没写楼层的柱平面等", "引用到的图": "主图上写了“详见”的图，why 写着从哪张图哪句话来",
                  "引用它的图": "没给楼层时才有：写了“详见”主图的图（详图用在哪）",
                  "同层其他图": "楼层、专业对得上，但图名没有关键词，按需看"},
    }


def _neighbors(st: dict, rels_by: dict, node: str, types: set[str], direction: str, start: bool) -> list[tuple]:
    """一个点往外一步：[(邻居, 关系, 方向)]。同层、通用图、共用参照只从起点那张图算（它们不是画在图上的关系，再往外就是全楼了）。"""
    out = []
    for rel in rels_by.get(node, ()):
        if rel["type"] not in types:
            continue
        if rel["from"] == node and direction in ("out", "both"):
            out.append((rel["to"], rel, "out"))
        elif rel["to"] == node and direction in ("in", "both"):
            out.append((rel["from"], rel, "in"))
    if start and types & set(FLOOR_BASED) and node in {s["no"] for s in st["sheets"]}:
        around = state.same_floor(st, node)
        for kind, rows in (("同层", around["same_floor"]), ("通用图", around["general"])):
            if kind in types:
                out += [(r["no"], {"type": kind, "from": node, "to": r["no"], "text": r["floors"] or kind,
                                   "page_id": None, "bbox": None}, "out") for r in rows]
        if "共用参照" in types:
            for name, nos in shared_xrefs(st).items():
                if node in nos:
                    out += [(x, {"type": "共用参照", "from": node, "to": x, "text": name, "page_id": None, "bbox": None}, "out")
                            for x in nos if x != node]
    return out


def shared_xrefs(st: dict) -> dict[str, list[str]]:
    """外部参照名 → 用了它的现行图（至少两张才算共用）。"""
    by: dict[str, list] = defaultdict(list)
    for s in st["sheets"]:
        if s["status"] != "voided":
            for name in s.get("xrefs") or ():
                by[name].append(s["no"])
    return {k: v for k, v in sorted(by.items()) if len(v) > 1}


def walk(st: dict, no: str, depth: int = 1, types: list[str] | None = None, direction: str = "both") -> dict:
    """从一张图（或一张变更单）出发，顺着关系往外走 depth 步。每个找到的图带距离和它是怎么连过来的：
    via 里 step_from 是上一步的图，edge 是这条关系本身（“A → B”是 A 上写了 B；变更单是“变更单 → 它改的图”），
    以及原文、出处页面和位置。direction：out 只走 edge 从当前图出去的，in 只走指向当前图的，both 都走。"""
    want = set(types or (*EXPLICIT, *FLOOR_BASED))
    bad = want - set(REL_LABEL.values())
    if bad:
        raise ValueError(f"关系种类可选：{'、'.join(REL_LABEL.values())}；不认识：{'、'.join(sorted(bad))}。")
    if direction not in ("out", "in", "both"):
        raise ValueError("direction 可选：out（它引用的）、in（引用它的）、both。")
    depth = max(1, min(int(depth), MAX_DEPTH))
    rels = relations(st)
    rels_by: dict[str, list] = defaultdict(list)
    for r in rels:
        rels_by[r["from"]].append(r)
        rels_by[r["to"]].append(r)
    by_no = {s["no"]: s for s in st["sheets"]}
    notices = {n["id"]: n for n in st["notices"]}

    def node_row(x: str) -> dict:
        if x in by_no:
            return dict(sheet_row(by_no[x]), kind="图纸")
        n = notices.get(x)
        if n:
            return {"no": x, "kind": n["kind"], "title": n["title"], "page_id": n["page_id"], "batch": n["batch"],
                    "changes": n["affects"]}
        return {"no": x, "kind": "图纸", "status": "缺失"}

    found: dict[str, dict] = {}
    frontier, truncated = [no], False
    for d in range(1, depth + 1):
        nxt = []
        for x in frontier:
            for y, rel, _way in _neighbors(st, rels_by, x, want, direction, x == no):
                if y == no:
                    continue
                if y not in found:
                    if len(found) >= MAX_NODES:
                        truncated = True
                        continue
                    if y in by_no and by_no[y]["status"] == "voided":
                        continue
                    found[y] = dict(node_row(y), distance=d, via=[])
                    nxt.append(y)
                if found[y]["distance"] == d:  # 只记最短的连法，同一步有几条都列出来
                    arrow = "↔" if rel["type"] in FLOOR_BASED else "→"
                    via = {"step_from": x, "relation": rel["type"], **({"form": rel["form"]} if rel.get("form") else {}),
                           "edge": f"{rel['from']} {arrow} {rel['to']}",
                           "text": rel["text"], **({"page_id": rel["page_id"]} if rel["page_id"] else {}),
                           **({"bbox": rel["bbox"]} if rel["bbox"] else {}), **({"mark": rel["mark"]} if rel.get("mark") else {}),
                           **({"target": rel["target"]} if rel.get("target") else {})}
                    if via not in found[y]["via"]:
                        found[y]["via"].append(via)
        frontier = nxt
    rows = sorted(found.values(), key=lambda r: (r["distance"], state._disc_order(r.get("discipline") or ""),
                                                 state.no_parts(r["no"])[0], state.no_parts(r["no"])[1] or 0, r["no"]))
    return {"start": node_row(no), "depth": depth, "types": sorted(want, key=list(REL_LABEL.values()).index),
            "direction": direction, "total": len(rows), "sheets": rows, **({"truncated": True} if truncated else {}),
            "missing": [r["no"] for r in rows if r.get("status") == "缺失"],
            "relation_types": {k: REL_NOTE[k] for k in want}, "forms": FORM_NOTE}


def page_owner(st: dict, page_id: str) -> dict | None:
    """一页是哪张图的哪一版（或哪张变更单）。"""
    for s in st["sheets"]:
        for v in s["versions"]:
            if v["page_id"] == page_id:
                return {"sheet": s["no"], "title": s["title"], "rev": v["rev"], "version_state": v["state"],
                        "current_rev": s["current_rev"], "current_page_id": s["current_page"]}
        for f in s.get("pending_files") or ():
            if f["page_id"] == page_id:
                return {"sheet": s["no"], "title": s["title"], "rev": f["rev"], "version_state": "还没看图识别",
                        "current_rev": s["current_rev"], "current_page_id": s["current_page"]}
    for n in st["notices"]:
        if n["page_id"] == page_id:
            return {"notice": n["id"], "title": n["title"], "kind": n["kind"], "changes": n["affects"]}
    return None


def page_relations(st: dict, page_id: str) -> list[dict]:
    """这一页上画出的关系（引用、目录条目），带位置：AI 可以挑一个，用 locate 跳过去。"""
    return [{"relation": r["type"], "form": r["form"], "to": r["to"], "text": r["text"], "bbox": r["bbox"],
             **({"mark": r["mark"]} if r["mark"] else {}), **({"target": r["target"]} if r["target"] else {})}
            for r in relations(st) if r["page_id"] == page_id and r["type"] != "变更单"]


def overlaps(a, b) -> bool:
    return bool(a and b) and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
