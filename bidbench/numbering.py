"""图号格式：每个项目的图号怎么分段、各段的代码什么意思（docs/图纸字段.md 9.5）。

软件先汇总文件名里四段以上的图号（overview），自己猜一个格式（guess，把握低）；AI 助手可以提议更准的格式；
人确认后（人工决定 numbering）才用来定楼栋、去掉版次后缀、认专业和楼层。确认之前只用来分组显示。

格式只记分段和码表，例如：
{"formats": [{"segments": ["project", "originator", "building", "discipline", "type", "level", "number", "rev?"],
              "example": "PRJ1-ORG-T1-ST-DWG-GF-1202-00"}],
 "codes": {"building": {"T1": "T1", "ZZ": "公共"}, "discipline": {"ST": "结构"}, "type": {"DET": "详图大样"},
           "level": {"GF": "Ground Floor", "XX": "不分层"}},
 "why": {"discipline.ST": "例图名都是 …… PLAN"}, "unsure": ["ES 是楼栋还是区域？"], "look_pages": ["3fa9c01b77de-p001"]}

带“?”的段可有可无，只能放在最后（版次、状态码）。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from . import classify, floors, names, towers

ROLES = {"project": "项目", "originator": "单位", "building": "楼栋", "zone": "区域", "discipline": "专业",
         "type": "类型", "level": "楼层", "number": "流水号", "rev": "版次", "status": "状态码", "other": "其他"}
TAIL_ROLES = {"rev", "status"}  # 只有这两种段可以可有可无，而且只能放在最后；它们不算图号本身
CODE_ROLES = ("building", "discipline", "type", "level")  # 码表只记这四类
TYPE_KINDS = ["目录说明", "平面图", "配筋图", "立面剖面", "详图大样", "系统图", "表格", "规格书", "计算书", "报告", "模型", "图纸"]
NO_LEVEL = "不分层"
MIN_ASK = 10  # 有这么多个四段以上、又不是 ISO 英国附录写法的图号，才问图号格式

CANDIDATE = re.compile(r"(?<![A-Z0-9-])([A-Z0-9]{1,10}(?:-[A-Z0-9]{1,10}){3,11})(?![A-Z0-9])", re.I)
NUMBER = re.compile(r"\d{2,6}[A-Z]?")
REV_SEG = re.compile(r"\d{1,3}|[PCTAD]\d{1,3}|[A-Z]")
SEG = re.compile(r"[A-Z0-9]{1,10}")
PAGE_ID = re.compile(r"[0-9a-f]{12}-p\d{3}")

# 软件自己猜格式时认得的代码（拿不准的不放：SE 可能是结构也可能是安防，FA 可能是幕墙也可能是火灾报警）
KNOWN_DISC = {"AR": "建筑", "A": "建筑", "ARC": "建筑", "ARCH": "建筑", "ST": "结构", "S": "结构", "STR": "结构",
              "ME": "暖通", "M": "暖通", "MV": "暖通", "HV": "暖通", "AC": "暖通", "EL": "电气", "E": "电气", "EE": "电气",
              "ELE": "电气", "PL": "给排水", "P": "给排水", "PH": "给排水", "PHE": "给排水", "D": "给排水",
              "FP": "消防", "FS": "消防", "IT": "智能化", "ICT": "智能化", "AV": "智能化", "BMS": "智能化", "ELV": "智能化",
              "SEC": "智能化", "TC": "智能化", "CSD": "机电", "MEP": "机电", "BS": "机电", "CV": "总图", "C": "总图",
              "CE": "总图", "CIV": "总图", "GE": "岩土", "GT": "岩土", "GEO": "岩土", "LA": "景观", "L": "景观",
              "LS": "景观", "ID": "室内", "I": "室内", "IN": "室内", "INT": "室内", "CW": "幕墙", "FC": "幕墙"}
KNOWN_TYPE = {"DWG": "图纸", "DR": "图纸", "DRG": "图纸", "SK": "图纸", "SKT": "图纸", "DET": "详图大样", "DT": "详图大样",
              "SCH": "表格", "SH": "表格", "BQ": "表格", "SP": "规格书", "SPC": "规格书", "SPEC": "规格书", "CA": "计算书",
              "CAL": "计算书", "CALC": "计算书", "RP": "报告", "REP": "报告", "RPT": "报告", "M2": "模型", "M3": "模型",
              "MOD": "模型"}
LEVEL_RX = re.compile(r"XX|ZZ|GF|LG|UG|LGF|UGF|RF|RL|LR|UR|MZ|B\d{1,2}|L\d{1,3}|\d{2}|P\d{1,2}")


class FormatError(ValueError):
    """格式写得不对。消息直接给 AI 或用户看。"""


def level_meaning(code: str | None) -> str | None:
    """软件自己认得的楼层代码怎么叫：GF → Ground Floor，01 → Level 01，B1 → Basement 01，RF → 屋面，XX → 不分层。"""
    c = (code or "").upper()
    if c in ("XX", "ZZ"):
        return NO_LEVEL
    if c in ("GF", "00"):
        return "Ground Floor"
    if c in ("RF", "RL", "R1"):
        return "屋面"
    m = re.fullmatch(r"B(\d{1,2})", c)
    if m and int(m.group(1)):
        return f"Basement {int(m.group(1)):02d}"
    m = re.fullmatch(r"L?(\d{1,3})", c)
    if m and int(m.group(1)):
        return f"Level {int(m.group(1)):02d}"
    return None


# ---------------- 从文件名里找候选图号 ----------------

def candidate(stem: str) -> tuple[str, str | None] | None:
    """文件名里四段以上的图号和它前后的图名：“PRJ1-ORG-T1-ST-DWG-GF-1202 - GROUND FLOOR PLAN” → (图号, 图名)。
    要有一段是两位以上的数字（流水号），还要有一段带字母。流水号后面只留版次、状态码这样的短段：
    图号和图名之间只用了“-”时（…-1202-GROUND FLOOR PLAN），GROUND 是图名的头一个词。"""
    for m in CANDIDATE.finditer(stem or ""):
        raw = m.group(1).split("-")
        parts = [p.upper() for p in raw]
        nums = [i for i, p in enumerate(parts) if NUMBER.fullmatch(p)]
        if not nums or not any(re.search(r"[A-Z]", p) for p in parts):
            continue
        flow = max((i for i in nums if len(parts[i].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) >= 3), default=nums[-1])
        keep = flow + 1  # 流水号是最后一段三位以上的数字；后面最多跟两段版次、状态码
        while keep < len(parts) and keep - flow <= 2 and (REV_SEG.fullmatch(parts[keep]) or names.STATUS.match(parts[keep])):
            keep += 1
        if keep < 4:
            continue
        rest = f"{stem[:m.start()]} {' '.join(raw[keep:])} {stem[m.end():]}"
        title = re.sub(r"\s+", " ", rest.replace("_", " ")).strip(" -–—.,，")
        return "-".join(parts[:keep]), title or None
    return None


def items_from(files) -> list[dict]:
    """files：[(出处路径, 第一页的 page_id 或 None)]，每个文件一条。返回带候选图号的：
    {"no": 文件名里的图号（各段照原样）, "parsed": 一级识别认出的图号, "title", "folders", "page_id"}。"""
    out = []
    for path, page_id in files:
        folders, stem, _page = names.split_path(path)
        c = candidate(stem)
        if c:
            out.append({"no": c[0], "parsed": names.parse_name(stem)["no"], "title": c[1], "folders": folders,
                        "page_id": page_id})
    return out


def need_count(items: list[dict]) -> int:
    """有几个文件的图号不是 ISO 英国附录的写法（按固定顺序读不了）。"""
    return sum(1 for it in items if not names.iso_parts(it["parsed"] or ""))


# ---------------- 汇总（给 AI 助手看） ----------------

def _shape(vals: Counter) -> str:
    keys = list(vals)
    if all(k.isdigit() for k in keys):
        lens = sorted({len(k) for k in keys})
        return f"数字（{lens[0]}{'–' + str(lens[-1]) if len(lens) > 1 else ''} 位）"
    if all(k.isalpha() for k in keys):
        lens = sorted({len(k) for k in keys})
        return f"字母（{lens[0]}{'–' + str(lens[-1]) if len(lens) > 1 else ''} 个）"
    return "字母和数字"


def _hint(vals: Counter, total: int) -> str | None:
    """软件看这一段像什么（给 AI 参考）。"""
    def share(test):
        return sum(c for v, c in vals.items() if test(v)) / max(total, 1)
    if len(vals) == 1:
        return "只有一个值"
    for word, test in (("像专业代码", lambda v: v in KNOWN_DISC), ("像类型代码", lambda v: v in KNOWN_TYPE),
                       ("像楼层代码", lambda v: bool(LEVEL_RX.fullmatch(v))), ("像流水号", lambda v: bool(NUMBER.fullmatch(v)))):
        if share(test) >= 0.6:
            return word
    return None


def overview(items: list[dict], max_values: int = 40, titles: int = 2) -> dict:
    """按段数分组：每一段出现过哪些值、各多少个文件，每个值附几个例图名和一个页面编号（用 get_page 看它的文字）。
    取值很多的段（流水号）只列几个例子。"""
    by_n: dict[int, list] = defaultdict(list)
    for it in items:
        by_n[len(it["no"].split("-"))].append(it)
    groups = []
    for n, its in sorted(by_n.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        g = {"parts": n, "files": len(its), "examples": [f"{it['no']}  {it['title'] or ''}".strip() for it in its[:6]]}
        if len(its) >= 3:
            segs = []
            for i in range(n):
                vals = Counter(it["no"].split("-")[i] for it in its)
                seg = {"position": i + 1, "distinct": len(vals), "shape": _shape(vals)}
                hint = _hint(vals, len(its))
                if hint:
                    seg["looks_like"] = hint
                if len(vals) <= max_values:
                    rows = []
                    for v, c in vals.most_common():
                        mine = [it for it in its if it["no"].split("-")[i] == v]
                        ts = list(dict.fromkeys(it["title"][:70] for it in mine if it["title"]))[:titles]
                        rows.append({"code": v, "files": c, "titles": ts,
                                     "page_id": next((it["page_id"] for it in mine if it["page_id"]), None)})
                    seg["values"] = rows
                else:
                    seg["examples"] = [v for v, _c in vals.most_common(10)]
                segs.append(seg)
            g["segments"] = segs
        groups.append(g)
    return {"files": len(items), "groups": groups}


def folder_summary(items: list[dict], depth: int = 3, limit: int = 40) -> list[dict]:
    """文件夹目录的概况：前几层文件夹各有多少个带图号的文件。"""
    count = Counter(" / ".join(it["folders"][:depth]) for it in items if it["folders"])
    return [{"folder": k, "files": c} for k, c in count.most_common(limit)]


def catalog_pages(items: list[dict], limit: int = 6) -> list[dict]:
    """目录、封面这类页：很多项目在这里写着图号编码说明。"""
    rx = re.compile(r"目录|封面|cover|drawing\s*(?:list|register|index)|document\s*register|legend|numbering", re.I)
    return [{"no": it["no"], "title": it["title"], "page_id": it["page_id"]}
            for it in items if it["page_id"] and rx.search(it["title"] or "")][:limit]


# ---------------- 软件自己猜 ----------------

def guess(items: list[dict]) -> dict | None:
    """看最多的那组图号每一段的取值像什么，猜一个格式（把握低）。猜不出返回 None。"""
    by_n: dict[int, list] = defaultdict(list)
    for it in items:
        by_n[len(it["no"].split("-"))].append(it["no"].split("-"))
    if not by_n:
        return None
    n, rows = max(by_n.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(rows) < 3:
        return None
    cols = [Counter(r[i] for r in rows) for i in range(n)]

    def share(i, test):
        return sum(c for v, c in cols[i].items() if test(v)) / len(rows)
    nums = [(len(cols[i]), i) for i in range(n) if share(i, lambda v: bool(NUMBER.fullmatch(v))) >= 0.9]
    if not nums:
        return None
    roles: list[str | None] = [None] * n
    ni = max(nums)[1]  # 取值最多的那段数字是流水号
    roles[ni] = "number"
    for i in range(ni + 1, n):  # 流水号后面的：两位以内的数字或 P01 这类，取值不多 → 版次
        roles[i] = "rev" if share(i, lambda v: bool(REV_SEG.fullmatch(v))) >= 0.9 and len(cols[i]) <= 30 else "other"
    free = [i for i in range(ni) if roles[i] is None]

    def level_like(i):  # 只有两位数字的一段，短图号里多半是系列号（ST-T1-05-013 的 05），带 GF、B1、L09 这类写法或 ISO 那么长才算楼层
        return n >= 6 or any(LEVEL_RX.fullmatch(v) and not v.isdigit() for v in cols[i])
    for role, test in (("discipline", lambda v: v in KNOWN_DISC), ("type", lambda v: v in KNOWN_TYPE),
                       ("level", lambda v: bool(LEVEL_RX.fullmatch(v)))):
        many = role != "type"  # 专业、楼层只有一个值的多半是项目、单位代码（ARC），类型可以全项目一个（DR）
        best = max(((share(i, test), -i) for i in free if (not many or len(cols[i]) >= 2) and (role != "level" or level_like(i))),
                   default=(0, 0))
        if best[0] >= 0.6:
            roles[-best[1]] = role
            free.remove(-best[1])
    order = ["project", "originator", "building"]  # 剩下的按先后：项目、单位、楼栋
    for i in list(free):
        if not order:
            break
        r = order.pop(0)
        if r == "project" and len(cols[i]) > 3 and order:
            r = order.pop(0)  # 第一段取值很多，不是项目代码
        roles[i] = r
    segs = [r or "other" for r in roles]
    example = rows[0]
    plus = by_n.get(n + 1, [])
    rev_like = sum(1 for r in plus if REV_SEG.fullmatch(r[-1]) and NUMBER.fullmatch(r[-2]))
    if segs[-1] == "number" and plus and rev_like >= 0.9 * len(plus):
        segs.append("rev?")  # 多一段的那组：多出来的是版次（可有可无）
        example = plus[0]  # 举例用带版次的，每一段都看得到
    elif segs[-1] == "rev" and by_n.get(n - 1):
        segs[-1] = "rev?"
    fmt = {"formats": [{"segments": segs, "example": "-".join(example)}], "codes": {}}
    seen: dict[str, Counter] = defaultdict(Counter)  # 码表按套得上的全部图号算（带版次和不带的两组都算）
    probe = Numbering(fmt)
    for it in items:
        m = probe.match(it["no"])
        for role in ("discipline", "type", "level"):
            if m and m["parts"].get(role):
                seen[role][m["parts"][role]] += 1
    for role, known in (("discipline", KNOWN_DISC.get), ("type", KNOWN_TYPE.get), ("level", level_meaning)):
        table = {v: known(v) for v in seen[role] if known(v)}
        if table:
            fmt["codes"][role] = table
    return fmt


# ---------------- 检查格式 ----------------

def validate(fmt) -> dict:
    """检查并整理一份格式（AI 提议的、人确认的）。不对时抛 FormatError。"""
    if not isinstance(fmt, dict):
        raise FormatError("格式应为 JSON 对象，含 formats（各种写法的分段）和 codes（代码的意思）。")
    fs = fmt.get("formats")
    if not isinstance(fs, list) or not fs:
        raise FormatError("formats 至少要有一种写法，每种写法是 {\"segments\": [...], \"example\": \"…\"}。")
    allowed = "、".join(f"{k}（{v}）" for k, v in ROLES.items())
    out_f = []
    for f in fs[:6]:
        segs = f.get("segments") if isinstance(f, dict) else None
        if not isinstance(segs, list) or not 3 <= len(segs) <= 12:
            raise FormatError("每种写法的 segments 是 3 到 12 段的列表，例如 [\"project\", \"originator\", \"building\", "
                              "\"discipline\", \"type\", \"level\", \"number\", \"rev?\"]。")
        clean, tail = [], False
        for s in segs:
            s = str(s).strip().lower()
            opt, r = s.endswith("?"), s.rstrip("?")
            if r not in ROLES:
                raise FormatError(f"不认识的段“{s}”。可用的段：{allowed}；可有可无的版次、状态码写成 rev?、status?。")
            if opt and r not in TAIL_ROLES:
                raise FormatError("只有版次（rev）、状态码（status）可以写成可有可无（加“?”）。")
            if tail and not opt:
                raise FormatError("可有可无的段只能放在最后。")
            tail = tail or opt
            clean.append(s)
        roles = [x.rstrip("?") for x in clean]
        if roles.count("number") != 1:
            raise FormatError("每种写法要有且只有一段流水号（number）。")
        for r in ("building", "discipline", "type", "level", "rev"):
            if roles.count(r) > 1:
                raise FormatError(f"{ROLES[r]}（{r}）只能有一段。")
        if any(r in TAIL_ROLES for r in roles[:roles.index("number")]):
            raise FormatError("版次、状态码只能在流水号后面。")
        item = {"segments": clean, "example": str(f.get("example") or "").strip().upper()[:80]}
        if f.get("label"):
            item["label"] = str(f["label"]).strip()[:20]
        out_f.append(item)
    codes = fmt.get("codes") or {}
    if not isinstance(codes, dict):
        raise FormatError("codes 应为 {\"building\": {...}, \"discipline\": {...}, \"type\": {...}, \"level\": {...}}。")
    out_c: dict[str, dict] = {}
    for role, table in codes.items():
        if role not in CODE_ROLES:
            raise FormatError("codes 只记 building（楼栋）、discipline（专业）、type（类型）、level（楼层）四类代码的意思。")
        if not isinstance(table, dict):
            raise FormatError(f"codes.{role} 应为 {{代码: 意思}}。")
        t = {}
        for code, meaning in list(table.items())[:300]:
            code = str(code).strip().upper()
            meaning = str(meaning if meaning is not None else "").strip()[:40]
            if not SEG.fullmatch(code):
                raise FormatError(f"代码“{code}”不对：只能是字母和数字。")
            if role == "discipline" and meaning not in classify.DISCIPLINES:
                raise FormatError(f"专业代码 {code} 的意思要写成这些之一：{'、'.join(classify.DISCIPLINES)}。")
            if role == "type" and meaning and meaning not in TYPE_KINDS and meaning != "其他":
                raise FormatError(f"类型代码 {code} 的意思要写成这些之一：{'、'.join(TYPE_KINDS)}、其他。")
            if role == "level" and meaning and meaning != NO_LEVEL and not floors.parse_floors(meaning, 1):
                raise FormatError(f"楼层代码 {code} 的意思“{meaning}”看不出是第几层：写成 Ground Floor、Level 01、Basement 01、"
                                  f"屋面，或“{NO_LEVEL}”。")
            t[code] = meaning
        out_c[role] = t
    out = {"formats": out_f, "codes": out_c}
    why = fmt.get("why")
    if isinstance(why, dict) and why:
        out["why"] = {str(k).strip()[:40]: str(v).strip()[:300] for k, v in list(why.items())[:150]}
    unsure = fmt.get("unsure")
    if isinstance(unsure, list) and unsure:
        out["unsure"] = [str(x).strip()[:200] for x in unsure[:20] if str(x).strip()]
    look = fmt.get("look_pages")
    if isinstance(look, list) and look:
        out["look_pages"] = [x for x in dict.fromkeys(str(y).strip() for y in look[:30]) if PAGE_ID.fullmatch(x)]
    return out


def decision_value(value: str) -> str:
    """人工决定 numbering 的值：“off”（先不用）或一份格式的 JSON，整理好再存。不对时抛 FormatError。"""
    if value == "off":
        return value
    try:
        fmt = json.loads(value)
    except ValueError:
        raise FormatError("图号格式应为 JSON。") from None
    return json.dumps(validate(fmt), ensure_ascii=False, sort_keys=True)


def describe(fmt: dict) -> str:
    """写成一句话：项目-单位-楼栋-专业-类型-楼层-流水号（-版次）。"""
    out = []
    for f in fmt.get("formats") or []:
        req = [ROLES[s] for s in f["segments"] if not s.endswith("?")]
        opt = [ROLES[s.rstrip("?")] for s in f["segments"] if s.endswith("?")]
        out.append("-".join(req) + "".join(f"（-{x}）" for x in opt))
    return "；".join(out)


# ---------------- 按格式拆图号 ----------------

def _seg_ok(role: str, v: str) -> bool:
    if role == "number":
        return bool(NUMBER.fullmatch(v))
    if role == "rev":
        return bool(REV_SEG.fullmatch(v))
    return bool(SEG.fullmatch(v))


class Numbering:
    """一份格式（已确认的、AI 提议的或软件猜的），按它拆图号。status：confirmed / proposed / guess。"""

    def __init__(self, fmt: dict, status: str = "guess", source: str = ""):
        self.fmt = fmt
        self.status = status
        self.source = source
        self.confirmed = status == "confirmed"
        self.codes = fmt.get("codes") or {}
        self._cache: dict = {}

    def match(self, no: str | None) -> dict | None:
        """{"parts": {段: 值}, "base": 去掉版次、状态码的图号, "rev": 版次或 None, "format": 第几种写法}；套不上返回 None。"""
        key = re.sub(r"\s+", "", no or "").upper()
        if key in self._cache:
            return self._cache[key]
        parts = key.split("-") if key else []
        out = None
        for k, f in enumerate(self.fmt.get("formats") or []):
            segs = f["segments"]
            req = sum(1 for s in segs if not s.endswith("?"))
            if not req <= len(parts) <= len(segs):
                continue
            roles = [s.rstrip("?") for s in segs[:len(parts)]]
            if all(_seg_ok(r, v) for r, v in zip(roles, parts)):
                got: dict[str, str] = {}
                for r, v in zip(roles, parts):
                    got.setdefault(r, v)
                out = {"parts": got, "base": "-".join(v for r, v in zip(roles, parts) if r not in TAIL_ROLES),
                       "rev": got.get("rev"), "format": k}
                break
        self._cache[key] = out
        return out

    def code(self, no: str | None, role: str) -> str | None:
        m = self.match(no)
        return m["parts"].get(role) if m else None

    def meaning(self, role: str, code: str | None) -> str | None:
        return (self.codes.get(role) or {}).get(code) if code else None

    def tower(self, no: str | None) -> str | None:
        """楼栋段 → 塔楼：码表里写了的照码表（“公共”归公共，空着表示这一段说的不是楼栋）；
        没写的照代码当楼栋名（T01 写成 T1），ZZ、XX、00 不算。"""
        return self.tower_name(self.code(no, "building"))

    def tower_name(self, c: str | None) -> str | None:
        if not c:
            return None
        m = self.meaning("building", c)
        if m is not None:
            return m or None
        if c in ("ZZ", "XX", "00"):
            return None
        return towers.from_folder(c) or c

    def discipline(self, no: str | None) -> str | None:
        m = self.meaning("discipline", self.code(no, "discipline"))
        return m if m and m != "其他" else None

    def level(self, no: str | None) -> tuple[str, str | None] | None:
        """(楼层代码, 意思)；意思是码表里写的，没写的按软件认得的写法。"""
        c = self.code(no, "level")
        if not c:
            return None
        return c, self.meaning("level", c) or level_meaning(c)

    def kind(self, no: str | None) -> str | None:
        """类型段说的图类（详图大样、表格、规格书……）；只说“图纸”的不算。"""
        m = self.meaning("type", self.code(no, "type"))
        return m if m and m not in ("图纸", "其他") else None

    def coverage(self, items: list[dict]) -> dict:
        """套一遍文件名里的图号：套得上多少个文件、套不上的例子、没写意思的代码。"""
        miss = [it["no"] for it in items if not self.match(it["no"])]
        unknown: dict[str, Counter] = defaultdict(Counter)
        for it in items:
            m = self.match(it["no"])
            if not m:
                continue
            for role in ("discipline", "type", "level"):
                v = m["parts"].get(role)
                if v and v not in (self.codes.get(role) or {}) and not (role == "level" and level_meaning(v)):
                    unknown[role][v] += 1
        return {"total": len(items), "matched": len(items) - len(miss), "unmatched": len(miss),
                "unmatched_examples": sorted(set(miss))[:10],
                "unknown_codes": {r: [k for k, _c in c.most_common(20)] for r, c in unknown.items()}}

    def codes_seen(self, items: list[dict], limit: int = 40) -> dict:
        """各类代码出现过哪些、各多少个文件，带上码表里的意思（界面上列码表用）。"""
        seen: dict[str, Counter] = defaultdict(Counter)
        for it in items:
            m = self.match(it["no"])
            for role in CODE_ROLES:
                v = m and m["parts"].get(role)
                if v:
                    seen[role][v] += 1
        out = {}
        for role, c in seen.items():
            rows = []
            for v, n in c.most_common(limit):
                mean = self.meaning(role, v)
                if mean is None and role == "level":
                    mean = level_meaning(v)
                if mean is None and role == "building":
                    mean = self.tower_name(v)
                rows.append({"code": v, "files": n, "meaning": mean})
            out[role] = rows
        return out

