"""楼层识别与覆盖检查用的工具：从图名、说明里读出覆盖了哪些楼层。

楼层表示：地上用正整数（1 = 1F），地下用负整数（-1 = B1），屋顶用 ROOF。

英文图名（Ground Floor、Level 05、Basement 01、Roof）也认。英式写法 Level 00 是首层（1F），Level 01 是 2F；
项目里没有 Ground、Level 00 时按 Level 01 是首层算（base=0），由调用方判断后传入。
"""

from __future__ import annotations

import re

ROOF = 1000  # 屋顶，排在所有地上楼层之后

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
NUM = r"(?:\d{1,3}|[零〇一二两三四五六七八九十]{1,4})"


def cn_int(s: str) -> int | None:
    """阿拉伯数字或中文数字（到九十九）转整数。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN and c != "十" for c in s):
        return None
    if "十" not in s:
        return _CN[s] if len(s) == 1 else None
    tens, _, ones = s.partition("十")
    t = _CN.get(tens, None) if tens else 1
    o = _CN.get(ones, None) if ones else 0
    if t is None or o is None:
        return None
    return t * 10 + o


def _norm(text: str) -> str:
    return (text.replace("～", "~").replace("－", "-").replace("—", "-").replace("–", "-")
            .replace("（", "(").replace("）", ")").replace("Ｆ", "F").replace(" ", ""))


RANGE = re.compile(rf"(?<![\dB\-])({NUM})(?:F|层)?(?:~|-|至|到)({NUM})(?:F|层)")
BASEMENT_RANGE = re.compile(rf"(?:地下|负|B|-)({NUM})(?:F|层)?(?:~|-|至|到)(?:地下|负|B|-)?({NUM})(?:F|层)")
BASEMENT = re.compile(rf"(?:地下|负)({NUM})层|(?<![A-Za-z])B({NUM})(?:F|层)?(?![\d版])|(?<![\d\w])-({NUM})(?:F|层)")
SINGLE_F = re.compile(r"(?<![\d.\-~A-Za-z])(\d{1,3})F(?![A-Za-z])")
SINGLE_CN = re.compile(rf"(?<!地下)(?<!负)(?<![\d~\-])({NUM})层(?!高)")


_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
EN_GROUND = re.compile(r"\b(?:ground\s*(?:floor|level|flr)?|G/F|GF|UGF|(?:level|lvl|L)\s*0{1,2})(?![\w/])", re.I)
EN_RANGE = re.compile(r"\b(?:levels?|lvls?|floors?|L)\s*0*(\d{1,3})\s*(?:-|–|~|to)\s*(?:levels?\s*|L)?0*(\d{1,3})\b", re.I)
EN_BASEMENT = re.compile(r"\b(?:basement|level\s*B|LB|B)\s*0*(\d{1,2})\b", re.I)
EN_LEVEL = re.compile(r"\b(?:level|lvl|L)\s*0*(\d{1,3})\b", re.I)
EN_ORDINAL = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)|(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth))"
                        r"\s*(?:floor|level|flr|storey)\b", re.I)
EN_ROOF = re.compile(r"\broof\b|(?<![A-Za-z])RF(?![A-Za-z])", re.I)


def is_ground_zero(text: str) -> bool:
    """英文图名、楼层代码里有 Ground Floor、Level 00、GF：按英式写法，Level 01 是 2F。"""
    return bool(EN_GROUND.search(text or ""))


def parse_floors_en(text: str, base: int = 1) -> set[int]:
    """英文图名里的楼层。base=1：Level 00 / Ground 是 1F、Level 01 是 2F；base=0：Level 01 是 1F。"""
    out: set[int] = set()
    t = text or ""
    if EN_ROOF.search(t):
        out.add(ROOF)
    if EN_GROUND.search(t):
        out.add(1)
    t = EN_GROUND.sub(" ", t)
    for m in EN_RANGE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b <= 200:
            out |= set(range(a + base, b + base + 1))
    t = EN_RANGE.sub(" ", t)
    for m in EN_BASEMENT.finditer(t):
        if int(m.group(1)):
            out.add(-int(m.group(1)))
    if re.search(r"\bbasement\b", t, re.I) and not EN_BASEMENT.search(t):
        out.add(-1)
    t = EN_BASEMENT.sub(" ", t)
    for m in EN_LEVEL.finditer(t):
        n = int(m.group(1)) + base
        if 0 < n <= 200:
            out.add(n)
    for m in EN_ORDINAL.finditer(t):
        n = int(m.group(1)) if m.group(1) else _ORD[m.group(2).lower()]
        out.add(n + base)
    return {f for f in out if f != 0}


def parse_floors(text: str, base: int = 1) -> set[int]:
    """从一段文字（图名或说明）里读出楼层集合。读不出返回空集合。base 只管英文写法，见 parse_floors_en。"""
    out: set[int] = parse_floors_en(text, base) if re.search(r"[A-Za-z]{2}", text or "") else set()
    t = _norm(text)
    if re.search(r"屋顶|屋面|机房层", t) and not re.search(r"屋面标高|屋面排水|屋面做法", t):
        out.add(ROOF)
    if re.search(r"首层|底层", t):
        out.add(1)
    for m in BASEMENT_RANGE.finditer(t):
        a, b = cn_int(m.group(1)), cn_int(m.group(2))
        if a and b:
            out |= {-n for n in range(min(a, b), max(a, b) + 1)}
    t = BASEMENT_RANGE.sub(" ", t)
    for m in BASEMENT.finditer(t):
        n = cn_int(next(g for g in m.groups() if g))
        if n:
            out.add(-n)
    t = BASEMENT.sub(" ", t)
    for m in RANGE.finditer(t):
        a, b = cn_int(m.group(1)), cn_int(m.group(2))
        if a and b and a <= b <= 200:
            out |= set(range(a, b + 1))
    t = RANGE.sub(" ", t)
    for m in SINGLE_F.finditer(t):
        out.add(int(m.group(1)))
    for m in SINGLE_CN.finditer(t):
        n = cn_int(m.group(1))
        if n and n <= 200:
            out.add(n)
    return {f for f in out if f != 0}


def label(f: int) -> str:
    if f == ROOF:
        return "屋顶"
    return f"B{-f}" if f < 0 else f"{f}F"


def compress(floors) -> str:
    """把楼层集合写成“B2、B1、1F、4F~20F、屋顶”这样的短文本：连续 3 层以上合并成区间。"""
    fs = sorted(set(floors))
    runs: list[list[int]] = []
    for f in fs:
        if runs and f == runs[-1][-1] + 1 and f != ROOF:
            runs[-1].append(f)
        else:
            runs.append([f])
    parts = []
    for r in runs:
        parts += [f"{label(r[0])}~{label(r[-1])}"] if len(r) >= 3 else [label(x) for x in r]
    return "、".join(parts)


# (专业, 图类名称, 图名需匹配, 图名需排除, 是否按总层数检查完整性)
GROUPS = [
    ("建筑", "建筑平面图", r"平面图", r"总平面|总图|详图|大样", True),
    ("结构", "结构梁配筋图", r"梁.*(配筋|平法)", None, False),
    ("结构", "结构板配筋图", r"板.*配筋|楼板", None, False),
    ("给排水", "给排水平面图", r"平面图", r"总平面", False),
    ("暖通", "暖通平面图", r"平面图", r"总平面", False),
    ("电气", "电气平面图", r"平面图", r"总平面", False),
]
STANDARD_HINT = re.compile(r"适用楼层|适用于|标准层|楼层范围")
