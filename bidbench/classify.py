"""智能分类：让 AI（Codex 或 Claude）给图纸分专业、分小类、起中文简称，给项目事实分多级目录。

只发图号、图名、所在文件夹，以及事实的名称和示例数值，不发图纸页面。AI 的回答存进 classifications 表
（同一张图、同一类事实以最后一次为准），state 计算时套用：图纸的专业只在按图号认不出（“其他”）时用 AI 的；
图谱里的小框、方块上的简称和思维导图里事实的大类、小类按 AI 的显示。还没分到的照旧按规则显示。
识别完新页面后自动对新出现的图纸和事实分一次；不满意可以全部重新分。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .store import Project, now_iso

logger = logging.getLogger(__name__)

DISCIPLINES = ["建筑", "结构", "给排水", "暖通", "电气", "机电", "总图", "景观", "幕墙", "室内", "其他"]
FACT_TOPS = ["工程概况", "楼层", "轴网", "结构构件", "材料", "建筑", "机电", "执行规范", "其他"]
STANDARD_TOP = "执行规范"
SHEET_BATCH = 120     # 一次问多少张图
FACT_BATCH = 150      # 一次问多少类事实
WORKERS = 3           # 第一批先问，定下小类的叫法；其余几批同时问
LABEL_MAX = 12        # 小类名称最多几个字（提示里要 2~6 个字，这里只防过长）
SHORT_MAX = 10        # 图纸简称最多几个字
KNOWN_MAX = 200       # 提示里附上的已有小类最多几条
EFFORT = "medium"
CLAUDE_MODEL = "claude-sonnet-5-5"

SYSTEM = "你是建筑工程投标图纸的整理员，帮投标人把图纸和从图纸上识别出的工程事实分门别类，严格按给定的 JSON 格式输出。"
SHEET_RULES = """下面每行是一张图：编号|图号|图名|所在文件夹。请为每张图给出：
- discipline：专业，只能从这些里选：{disc}。依据图号里的专业代码（例如 ISO 19650 编号里的角色代码 A 建筑、S 结构、M 暖通、E 电气、P 给排水，或 ARC、STR、MEP 这类代码）、图名和所在文件夹判断；说不准的填 其他。
- group：这张图在本专业里的小类，2~6 个汉字，把明显同类的图归在一起，例如 目录说明、平面图、立面图、剖面图、楼梯详图、墙身大样、门窗表、梁配筋、板配筋、系统图。各楼层的平面图归进同一个小类，不要按楼层拆开；一个专业的小类一般 3~10 个，不要一张图一个小类。
- short：图名的中文简称，不超过 8 个字，要能和同专业的其他图区分开，例如 Level 02 GA Plan → 2层平面，Stair 1 Details → 1号楼梯详图，North Elevation → 北立面。图名本来就是不超过 8 个字的中文时填 null。"""
FACT_RULES = """下面每行是一类事实：编号|名称（同一类的几个例子，用；隔开）|示例数值|条数。名称前标〔规范〕的是图上引用的规范标准。请为每一类给出：
- top：大类，只能从这些里选：{tops}。规范标准一律放 执行规范，其他事实不要放 执行规范。
- sub：大类下面的小类，从粗到细 0~2 级，每级 2~6 个汉字，例如 楼层 → ["层高"]、["标高"]；结构构件 → ["梁", "截面"]、["墙", "墙厚"]；建筑 → ["门窗", "樘数"]；执行规范 → ["国家标准"]、["图集"]。同类的事实放进同一个小类，叫法前后一致；一般给 1 级，同一小类里的事实很多时再给第 2 级；实在不好归类的填空数组。"""
TAIL = "每一行都要给出，i 填行首的编号。这是一次整理任务：不要读写任何文件，不要运行任何命令。只输出 JSON。"

SCHEMAS = {
    "sheet": {"type": "object", "additionalProperties": False, "required": ["sheets"], "properties": {"sheets": {
        "type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["i", "discipline", "group", "short"],
            "properties": {"i": {"type": "integer"}, "discipline": {"type": "string", "enum": DISCIPLINES},
                           "group": {"type": "string"}, "short": {"type": ["string", "null"]}}}}}},
    "fact": {"type": "object", "additionalProperties": False, "required": ["facts"], "properties": {"facts": {
        "type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["i", "top", "sub"],
            "properties": {"i": {"type": "integer"}, "top": {"type": "string", "enum": FACT_TOPS},
                           "sub": {"type": "array", "items": {"type": "string"}}}}}}},
}


# ---------------- 事实的写法 ----------------

SHEET_NOTE = re.compile(r"[（(][^（）()]*图[^（）()]*[）)]$")  # 名称末尾括注的出处图名，如“（三层梁配筋图）”
CN_FLOOR = re.compile(r"[零一二三四五六七八九十百两]+(?=层)")


def fact_template(name: str) -> str:
    """同一类事实的写法，智能分类按它记：去掉空格和末尾括注的出处图名，数字（含楼层的中文数字）换成 #。
    KL3 截面、KL12 截面（三层梁配筋图）都是 KL#截面，分一次就够，以后新识别出的同类事实直接套用。"""
    s = re.sub(r"\s+", "", name).replace("～", "~")
    s = SHEET_NOTE.sub("", s) or s
    s = CN_FLOOR.sub("#", s)
    return re.sub(r"\d+(?:\.\d+)?", "#", s)


def fact_family(name: str) -> str:
    """还没智能分类时的粗分：去掉编号、楼层和括注，剩下的说法。KL3 截面、Q1 截面 → 截面；1F 层高、3F~20F 层高 → 层高。"""
    s = re.sub(r"[（(][^（）()]*[）)]", " ", name)
    s = re.sub(r"[A-Za-z]*\d+(?:\.\d+)?[A-Za-z]*", " ", s)
    s = re.sub(r"[零一二三四五六七八九十百两]+层", " ", s)
    s = re.sub(r"[~～\-—_/、,，;；:：#·.]+", " ", s)
    s = re.sub(r"\s*([一-鿿])\s*", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or name.strip()


# ---------------- 读取、套用 ----------------

def load_labels(con) -> dict:
    """最新的分类：{"sheet": {图号: 分类}, "fact": {事实写法: 分类}, "last": 最近一次的执行器、模型、时间}。"""
    out: dict = {"sheet": {}, "fact": {}, "last": None}
    for r in con.execute("SELECT kind, key, label, executor, model, created_at FROM classifications ORDER BY id"):
        if r["kind"] not in ("sheet", "fact"):
            continue
        try:
            lab = json.loads(r["label"])
        except ValueError:
            continue
        if isinstance(lab, dict):
            out[r["kind"]][r["key"]] = lab
            out["last"] = {"executor": r["executor"], "model": r["model"], "at": r["created_at"]}
    return out


def sheet_discipline(lab, rule: str) -> str:
    """专业：按图号认得出的照旧；认不出（其他）的用 AI 给的。"""
    if rule == "其他" and isinstance(lab, dict) and lab.get("discipline") in DISCIPLINES:
        return lab["discipline"]
    return rule


def sheet_view(lab, rule: str) -> dict | None:
    """图谱、图纸卡片用：AI 给的小类和简称，专业是不是 AI 定的。"""
    if not isinstance(lab, dict):
        return None
    group = lab.get("group") if isinstance(lab.get("group"), str) and lab.get("group") else None
    short = lab.get("short") if isinstance(lab.get("short"), str) and lab.get("short") else None
    ai_disc = sheet_discipline(lab, rule) != rule
    if not (group or short or ai_disc):
        return None
    return {"group": group, "short": short, "discipline": ai_disc}


def fact_place(lab, std: bool) -> tuple[str, list[str]] | None:
    """AI 给事实的位置（大类, 小类）；不合格式的不用，照旧按规则。"""
    if not isinstance(lab, dict):
        return None
    top = STANDARD_TOP if std else lab.get("top")
    if top not in FACT_TOPS or (top == STANDARD_TOP and not std):
        return None
    sub = [x for x in (lab.get("sub") if isinstance(lab.get("sub"), list) else []) if isinstance(x, str) and x and x != top]
    return top, sub[:2]


def summary(labels: dict, st: dict) -> dict:
    """问题中心、思维导图工具栏上显示：分到了多少，上次是谁、什么时候分的。"""
    facts = [f for g in st["facts"]["groups"] for f in g["facts"]]
    return {"sheets": len(st["sheets"]), "sheets_done": sum(1 for s in st["sheets"] if s["no"] in labels["sheet"]),
            "facts": len(facts), "facts_done": sum(1 for f in facts if f.get("ai")), "last": labels["last"]}


# ---------------- 要分的 ----------------

def _folder(paths: list) -> str:
    """所在文件夹：去掉批次和文件名，只留中间几层（最后 60 个字）。"""
    if not paths:
        return ""
    return " / ".join(paths[0].split(" / ")[1:-1])[-60:]


def pending(st: dict, done: dict, redo: bool = False) -> tuple[list[dict], list[dict]]:
    """要分类的图纸和事实类别：没分过的、图名变了的；redo 时全部重新分。"""
    sheets = []
    for s in st["sheets"]:
        lab = done["sheet"].get(s["no"])
        if redo or not isinstance(lab, dict) or lab.get("title") != (s["title"] or ""):
            sheets.append({"key": s["no"], "no": s["no"], "title": s["title"] or "", "folder": _folder(s["paths"])})
    fams: dict[str, dict] = {}
    for g in st["facts"]["groups"]:
        for f in g["facts"]:
            t = fact_template(f["name"])
            x = fams.get(t)
            if x is None:
                x = fams[t] = {"key": t, "names": [], "value": str(f["value"]), "count": 0, "std": bool(f.get("std"))}
            x["count"] += 1
            if len(x["names"]) < 3 and f["name"] not in x["names"]:
                x["names"].append(f["name"])
    facts = [x for t, x in fams.items() if redo or t not in done["fact"]]
    facts.sort(key=lambda x: (x["std"], fact_family(x["names"][0]), x["key"]))  # 同类的排在一起，分在同一批
    return sheets, facts


# ---------------- 提示和回答 ----------------

def _cell(v) -> str:
    return re.sub(r"[|\r\n]+", " ", str(v or "")).strip()


def _known(kind: str, done: dict) -> dict:
    """已有的小类，提示里附上，前后几批、前后几次的叫法才一致。"""
    known: dict[str, set] = {}
    for lab in done[kind].values():
        if kind == "sheet":
            if isinstance(lab, dict) and lab.get("group") and lab.get("discipline") in DISCIPLINES:
                known.setdefault(lab["discipline"], set()).add(lab["group"])
        else:
            place = fact_place(lab, isinstance(lab, dict) and lab.get("top") == STANDARD_TOP)
            if place and place[1]:
                known.setdefault(place[0], set()).add(" / ".join(place[1]))
    return known


def _learn(known: dict, kind: str, labels: dict) -> None:
    for lab in labels.values():
        if kind == "sheet":
            known.setdefault(lab["discipline"], set()).add(lab["group"])
        elif lab["sub"]:
            known.setdefault(lab["top"], set()).add(" / ".join(lab["sub"]))


def _known_text(known: dict, order: list) -> str:
    lines, n = [], 0
    for k in sorted(known, key=lambda k: order.index(k) if k in order else len(order)):
        vals = sorted(known[k])[:max(0, KNOWN_MAX - n)]
        if vals:
            lines.append(f"{k}：{'、'.join(vals)}")
            n += len(vals)
    return "已有的小类，能归进去的就用原来的名字：\n" + "\n".join(lines) if lines else ""


def sheet_prompt(items: list[dict], known: dict) -> str:
    rows = "\n".join(f"{i}|{_cell(x['no'])}|{_cell(x['title']) or '（没有图名）'}|{_cell(x['folder'])}"
                     for i, x in enumerate(items, 1))
    return "\n\n".join(p for p in [SYSTEM, SHEET_RULES.format(disc="、".join(DISCIPLINES)),
                                   _known_text(known, DISCIPLINES), "图纸：\n" + rows, TAIL] if p)


def fact_prompt(items: list[dict], known: dict) -> str:
    rows = "\n".join(f"{i}|{'〔规范〕' if x['std'] else ''}{'；'.join(_cell(n) for n in x['names'])}|"
                     f"{_cell(x['value'])[:24]}|{x['count']}" for i, x in enumerate(items, 1))
    return "\n\n".join(p for p in [SYSTEM, FACT_RULES.format(tops="、".join(FACT_TOPS)),
                                   _known_text(known, FACT_TOPS), "事实：\n" + rows, TAIL] if p)


def _label(v, n: int) -> str | None:
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", v).strip(" /／|·.。、")
    return v[:n] or None


def clean(kind: str, body, items: list[dict]) -> dict[int, dict]:
    """AI 的回答 → {行号: 分类}。不合格式的行丢掉：那几项照旧按规则显示，下次再分。"""
    rows = body.get("sheets" if kind == "sheet" else "facts") if isinstance(body, dict) else None
    out: dict[int, dict] = {}
    for x in rows if isinstance(rows, list) else []:
        if not isinstance(x, dict):
            continue
        i = x.get("i")
        if not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= len(items) or i in out:
            continue
        it = items[i - 1]
        if kind == "sheet":
            group = _label(x.get("group"), LABEL_MAX)
            if group:
                out[i] = {"discipline": x.get("discipline") if x.get("discipline") in DISCIPLINES else "其他",
                          "group": group, "short": _label(x.get("short"), SHORT_MAX), "title": it["title"]}
            continue
        top = STANDARD_TOP if it["std"] else x.get("top")
        if top not in FACT_TOPS or (top == STANDARD_TOP and not it["std"]):
            continue
        subs = x.get("sub") if isinstance(x.get("sub"), list) else []
        out[i] = {"top": top, "sub": [s for s in (_label(s, LABEL_MAX) for s in subs[:2]) if s and s != top]}
    return out


# ---------------- 模拟（演示、测试用）----------------

_SHEET_WORDS = [(re.compile(r"目录|说明|封面|图例|cover|register|notes?\b|legend", re.I), "目录说明"),
                (re.compile(r"系统|schematic|riser", re.I), "系统图"),
                (re.compile(r"立面|剖面|elevation|section", re.I), "立面剖面"),
                (re.compile(r"详图|大样|节点|detail", re.I), "详图大样"),
                (re.compile(r"配筋|reinforcement", re.I), "配筋图"),
                (re.compile(r"表|schedule", re.I), "表格"),
                (re.compile(r"平面|布置|plan|layout", re.I), "平面图")]
ISO_ROLE = {"A": "建筑", "S": "结构", "M": "暖通", "H": "暖通", "E": "电气", "P": "给排水", "D": "给排水",
            "L": "景观", "I": "室内", "C": "总图"}


def rule_body(kind: str, items: list[dict]) -> dict:
    """模拟执行器：按规则分，不调用 AI，回答的格式和 AI 一样。"""
    from .state import discipline_of, fact_group
    if kind == "sheet":
        rows = []
        for i, x in enumerate(items, 1):
            d = discipline_of(x["no"])
            if d == "其他":
                m = re.search(r"-([A-Z])-\d{3,}[A-Z]?$", x["no"])  # ISO 19650 编号里的角色代码
                d = ISO_ROLE.get(m.group(1), "其他") if m else "其他"
            g = next((g for rx, g in _SHEET_WORDS if rx.search(x["title"])), "其他")
            rows.append({"i": i, "discipline": d, "group": g, "short": None})
        return {"sheets": rows}
    return {"facts": [{"i": i, "top": STANDARD_TOP if x["std"] else fact_group(x["names"][0], False),
                       "sub": [fact_family(x["names"][0])]} for i, x in enumerate(items, 1)]}


# ---------------- 运行 ----------------

def run(proj: Project, answer, executor: str, model: str | None, redo: bool = False, log=print, progress=None,
        workers: int = WORKERS) -> dict:
    """answer(kind, items, prompt) 返回 AI 的回答（dict）。分好一批存一批，工作台跟着更新。
    整次都会失败的错误（没登录、额度用完、密钥无效）抛 FatalError，已分好的保留。"""
    from . import state
    from .executors.claude import FatalError, RecognitionError
    st = state.build(proj, with_diff=False)
    with proj.connect() as con:
        done = load_labels(con)
    sheets, facts = pending(st, done, redo)
    total = len(sheets) + len(facts)
    res = {"sheets": 0, "facts": 0, "missed": 0, "executor": executor, "model": model}
    if not total:
        log("图纸和事实都已经分过类了。想全部重新分，勾选“全部重新分类”。")
        return res
    log(f"智能分类：{len(sheets)} 张图、{len(facts)} 类事实。只发图号、图名、所在文件夹和事实的名称、示例数值。")
    lock = threading.Lock()
    finished = 0

    def save(kind, items, labels, n):
        nonlocal finished
        rows = [(kind, items[i - 1]["key"], json.dumps(lab, ensure_ascii=False), executor, model, now_iso())
                for i, lab in sorted(labels.items())]
        with lock:
            if rows:
                with proj.connect() as con:
                    con.executemany("INSERT INTO classifications(kind, key, label, executor, model, created_at)"
                                    " VALUES (?,?,?,?,?,?)", rows)
            res["sheets" if kind == "sheet" else "facts"] += len(rows)
            res["missed"] += len(items) - len(rows)
            finished += len(items)
            what = "图纸" if kind == "sheet" else "事实"
            log(f"  {what}第 {n} 批：{len(items)} 项，分好 {len(rows)} 项")
            if progress:
                progress(finished, total)

    def one(kind, items, known, n):
        prompt = (sheet_prompt if kind == "sheet" else fact_prompt)(items, known)
        try:
            body = answer(kind, items, prompt)
        except FatalError:
            raise
        except Exception as e:  # noqa: BLE001 - 这一批没分成：照旧按规则显示，下次再分
            logger.info("智能分类有一批出错：%s", type(e).__name__)
            log(f"  ✗ 有一批（{len(items)} 项）没分成：{e if isinstance(e, RecognitionError) else f'{type(e).__name__}: {e}'}")
            save(kind, items, {}, n)
            return {}
        labels = clean(kind, body, items)
        save(kind, items, labels, n)
        return labels

    for kind, items, size in (("sheet", sheets, SHEET_BATCH), ("fact", facts, FACT_BATCH)):
        if not items:
            continue
        batches = [items[i:i + size] for i in range(0, len(items), size)]
        known = {} if redo else _known(kind, done)
        _learn(known, kind, one(kind, batches[0], known, 1))  # 第一批先定下叫法，其余几批同时问
        if len(batches) > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(one, kind, b, known, n) for n, b in enumerate(batches[1:], 2)]
                try:
                    for f in futures:
                        f.result()
                except BaseException:  # 额度用完等：还没开始的几批不再问
                    for f in futures:
                        f.cancel()
                    raise
    log(f"分好 {res['sheets']} 张图、{res['facts']} 类事实"
        + (f"；{res['missed']} 项没分成，照旧按规则显示，下次智能分类时再分。" if res["missed"] else "。"))
    return res
