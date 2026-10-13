"""塔楼（单体、子项）：从 ISO 图号的分区代码、文件夹名、文件名和图名、PDF 文字里认出是哪一栋（docs/图纸字段.md 9.1）。

只认写明“哪一栋”的写法：T1、Tower 1、塔楼1、1#楼、1号楼、A座、裙楼、地下室、公共。
文件名和图名里不认单独的 T1（也可能是版次），也不认裙楼、地下室（图名里多半说的是楼层，如“地下室顶板平面图”）。
同一种写法的不同写法自动并成一个（Tower 1、T01 → T1；1号楼 → 1#楼）；不同写法（T1 和 1#楼）是不是同一栋，问人。"""

from __future__ import annotations

import re

COMMON, UNKNOWN = "公共", "未分塔楼"
SHARED = {COMMON, "裙楼", "地下室"}  # 几栋共用：同层图纸跟每一栋都算同层，楼层覆盖只查断档

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

T_FOLDER = re.compile(r"(?i)^T0*([1-9]\d?)(?:[\s\-_.（(]|$)")                       # 文件夹叫 T1、T01、T1-建筑、T1.zip
TOWER_WORD = re.compile(r"(?i)(?<![A-Za-z])(?:tower|twr)\s*[-_]?\s*0*([1-9]\d?|[A-Z])(?![A-Za-z0-9])")  # Tower 1、Tower A
TOWER_CN = re.compile(r"塔楼\s*0*([1-9]\d?|[A-Z])(?![A-Za-z0-9])|0*([1-9]\d?)\s*号\s*塔楼")    # 塔楼1、1号塔楼
BUILDING = re.compile(r"(?<![0-9])0*([1-9]\d?|[一二三四五六七八九十]{1,3})\s*(?:[#＃]|号)\s*楼(?![梯板面层顶座])")  # 1#楼、1号楼、一号楼
BLOCK = re.compile(r"(?<![A-Za-z])([A-Z])\s*[座栋]")                               # A座、B栋
PODIUM = re.compile(r"(?i)裙楼|裙房|(?<![a-z])podium(?![a-z])")
BASEMENT = re.compile(r"(?i)地下室|地库|(?<![a-z])basement(?![a-z])")
COMMON_WORD = re.compile(r"(?i)^(?:公共|通用|公用|总体)|(?<![a-z])(?:common|general|shared)(?![a-z])")
SUBITEM = re.compile(r"(?:子项|单体)(?:名称)?[\s:：]+([^\s:：，,;；]{1,16})")


def _cn_int(s: str) -> int:
    """一 → 1，十 → 10，十二 → 12，二十 → 20。"""
    if s.isdigit():
        return int(s)
    if "十" not in s:
        return CN_DIGITS.get(s, 0)
    head, _, tail = s.partition("十")
    return (CN_DIGITS.get(head, 1) if head else 1) * 10 + (CN_DIGITS.get(tail, 0) if tail else 0)


def _t(x: str) -> str:
    return f"T{x.upper()}"


def from_text(text: str) -> str | None:
    """文件名、图名里写明的塔楼：Tower 1、塔楼1、1号塔楼、1#楼、A座。"""
    text = text or ""
    m = TOWER_WORD.search(text)
    if m:
        return _t(m.group(1))
    m = TOWER_CN.search(text)
    if m:
        return _t(m.group(1) or m.group(2))
    m = BUILDING.search(text)
    if m and _cn_int(m.group(1)):
        return f"{_cn_int(m.group(1))}#楼"
    m = BLOCK.search(text)
    if m:
        return f"{m.group(1)}座"
    return None


def from_folder(name: str) -> str | None:
    """文件夹（或压缩包）名里的塔楼；也认 T1、裙楼、地下室、公共这类只在文件夹里才可靠的写法。"""
    name = (name or "").strip()
    m = T_FOLDER.match(name)
    if m:
        return _t(m.group(1))
    t = from_text(name)
    if t:
        return t
    if PODIUM.search(name):
        return "裙楼"
    if BASEMENT.search(name):
        return "地下室"
    if COMMON_WORD.search(name):
        return COMMON
    return None


def from_iso(no: str | None) -> str | None:
    """ISO 19650 图号（项目-发起方-分区-楼层-类型-角色-编号）第三段的分区代码；ZZ（多个分区）、XX（不分区）不算。"""
    parts = (no or "").upper().split("-")
    if len(parts) != 7 or parts[2] in ("ZZ", "XX", "00", ""):
        return None
    m = re.fullmatch(r"T0*([1-9]\d?)", parts[2])
    return _t(m.group(1)) if m else parts[2]


def from_pdf_text(text: str) -> str | None:
    """PDF 文字里的“子项名称：1#楼”“单体：A座”。不是常见写法的（如“办公楼”）照原样当塔楼名。"""
    m = SUBITEM.search(text or "")
    if not m:
        return None
    v = m.group(1)
    return from_folder(v) or v


def alias_pairs(labels) -> list[tuple[str, str]]:
    """可能是同一栋楼的不同叫法：T1 和 1#楼、TA 和 A座。返回 (保留的叫法, 另一个叫法)。"""
    have = set(labels)
    out = []
    for t in sorted(have):
        m = re.fullmatch(r"T(\d+|[A-Z])", t)
        if not m:
            continue
        other = f"{m.group(1)}#楼" if m.group(1).isdigit() else f"{m.group(1)}座"
        if other in have:
            out.append((t, other))
    return out


def order_key(t: str | None):
    """塔楼的先后：T1、T2……、1#楼……、A座……，再是裙楼、地下室、公共，看不出的放最后。"""
    if not t:
        return (9, 0, "")
    m = re.fullmatch(r"T(\d+)", t) or re.fullmatch(r"(\d+)#楼", t)
    if m:
        return (0 if t.startswith("T") else 1, int(m.group(1)), t)
    rank = {"裙楼": 6, "地下室": 7, COMMON: 8}
    return (rank.get(t, 2), 0, t)
