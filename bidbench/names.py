"""一级识别：只看文件名、所在文件夹和 PDF 自带的文字，推出图号、图名、版次、专业、楼层，每项标来源和把握。

图号按 ISO 19650 的常见写法认：项目-发起方-分区-楼层-类型-角色-编号，如 PRJ1-ARC-ZZ-00-DR-A-1001；
文件名后面常跟状态码和版次（_S4_P01、[P01]、Rev C01）。也认 建施-01、A-101 这类写法。
推出来的只当暂定：看图识别（二级）的结果覆盖它，判断缺图、版本不用它。
"""

from __future__ import annotations

import re

HIGH, MID, LOW = "high", "mid", "low"

# ISO 19650（英国附录）的角色代码 → 专业
ROLE = {"A": "建筑", "S": "结构", "M": "暖通", "H": "暖通", "E": "电气", "P": "给排水", "D": "给排水",
        "C": "总图", "G": "总图", "T": "总图", "L": "景观", "I": "室内"}
ROLE2 = {"AR": "建筑", "ST": "结构", "SE": "结构", "ME": "暖通", "MV": "暖通", "HV": "暖通", "EE": "电气", "EL": "电气",
         "PH": "给排水", "PL": "给排水", "FP": "给排水", "LA": "景观", "LS": "景观", "ID": "室内", "IN": "室内",
         "FA": "幕墙", "FC": "幕墙", "CW": "幕墙", "CE": "总图", "CV": "总图"}
# 发起方代码、文件夹名里的专业词
WORDS = [
    (re.compile(r"消防|fire\s*(?:alarm|protection|fighting|suppression|sprinkler)", re.I), "消防"),
    (re.compile(r"智能化|\bICT\b|\bELV\b|\bBMS\b|\bAV\b|audio\s*visual|security|cctv", re.I), "智能化"),
    (re.compile(r"岩土|勘察|geotech", re.I), "岩土"),
    (re.compile(r"幕墙|facade|façade|curtain\s*wall|envelope|cladding", re.I), "幕墙"),
    (re.compile(r"景观|landscap", re.I), "景观"),
    (re.compile(r"室内|装修|interior", re.I), "室内"),
    (re.compile(r"总图|总平面|civil|infrastructure|external\s*works|site\s*works", re.I), "总图"),
    (re.compile(r"结构|structur|\bSTR\b|\bSTRC?\b|\bSE\b", re.I), "结构"),
    (re.compile(r"给排水|给水|排水|plumbing|public\s*health|drainage|\bPHE?\b|\bPLB\b", re.I), "给排水"),
    (re.compile(r"暖通|通风|空调|hvac|mechanical|\bMECH?\b|\bMEC\b", re.I), "暖通"),
    (re.compile(r"电气|强电|弱电|electrical|\bELEC?\b|\bELE\b", re.I), "电气"),
    (re.compile(r"机电|\bMEP\b|\bMEPF\b|building\s*services|services", re.I), "机电"),
    (re.compile(r"建筑|architect|\bARCH?\b|\bARC\b", re.I), "建筑"),
]
CN_PREFIX = [("建", "建筑"), ("结", "结构"), ("水", "给排水"), ("暖", "暖通"), ("电", "电气")]

EXT = re.compile(r"\.(?:pdf|dwg|dxf|dwf|tiff?|png|jpe?g)$", re.I)
PAGE = re.compile(r"（第 (\d+) 页）$")
REV = re.compile(r"^(?:[PCTAD]\d{1,3}(?:\.\d{1,2})?|[A-Z]\d?)$")        # 图号后面紧跟的版次：P01、C02、T1、P01.1、C
REV_STRICT = re.compile(r"^[PCT]\d{2,3}(?:\.\d{1,2})?$")                  # 图名里也认的版次：P01、C02（B2、A1 是楼层、轴号）
REV_WORD = re.compile(r"(?i)\brev(?:ision)?\.?\s*[:：]?\s*([A-Z]{0,2}\d{0,3}(?:\.\d{1,2})?)\b")
STATUS = re.compile(r"^(?:S[0-7]|A[0-7]|B[1-7]|CR|D[1-4])$")           # 状态码（适用性）
ISO_NO = re.compile(r"(?<![A-Z0-9-])([A-Z0-9]{1,8}(?:-[A-Z0-9]{1,8}){4,9})(?![A-Z0-9])", re.I)
CN_NO = re.compile(r"^([一-鿿]{1,3}施)\s*[-－—_]?\s*(\d{1,3}[A-Za-z]?)")
SIMPLE_NO = re.compile(r"^([A-Z]{1,3})[-－—](\d{2,4}[A-Z]?)(?![\d-])")
TEXT_LABEL_NO = re.compile(r"(?:图号|图纸编号|drawing\s*(?:no|number)\.?)\s*[:：]?\s*([A-Z0-9一-鿿][A-Z0-9一-鿿\-－—_.]{1,40})", re.I)


def iso_parts(no: str | None) -> dict | None:
    """ISO 19650 写法的图号拆开：项目、发起方、楼层、类型、角色、编号；不是这种写法返回 None。"""
    parts = (no or "").upper().split("-")
    if len(parts) < 5 or not re.fullmatch(r"\d{2,6}[A-Z]?", parts[-1]) or not re.fullmatch(r"[A-Z]{1,2}", parts[-2]) \
            or not re.fullmatch(r"[A-Z0-9]{2}", parts[-3]):
        return None
    return {"project": parts[0], "originator": parts[1], "level": parts[-4] if len(parts) >= 6 else None,
            "type": parts[-3], "role": parts[-2], "number": parts[-1]}


def role_discipline(no: str | None) -> str | None:
    p = iso_parts(no)
    if not p:
        return None
    return ROLE2.get(p["role"]) or ROLE.get(p["role"][0])


def words_discipline(text: str) -> str | None:
    for rx, d in WORDS:
        if rx.search(text or ""):
            return d
    return None


def split_path(path: str) -> tuple[list[str], str, int | None]:
    """出处路径 → (中间的文件夹, 文件名去掉扩展名, 第几页)。“批次#1 / 外层 / 结构 / 结施-01.pdf（第 2 页）”"""
    parts = [x for x in re.split(r"\s+/\s+", path or "") if x]
    name = parts[-1] if parts else ""
    m = PAGE.search(name)
    page = int(m.group(1)) if m else None
    name = EXT.sub("", PAGE.sub("", name)).strip()
    return [x for x in parts[1:-1]], name, page


def parse_name(stem: str) -> dict:
    """文件名 → 图号、版次、状态码、图名（都可能为 None）。"""
    out = {"no": None, "kind": None, "rev": None, "status": None, "title": None}
    s = stem.replace("＿", "_").strip()
    title = s
    m = ISO_NO.search(s.upper())
    if m:
        parts = m.group(1).upper().split("-")
        orig, words = s[m.start():m.end()].split("-"), []  # orig：原样的大小写
        while parts and not re.fullmatch(r"\d{2,6}[A-Z]?", parts[-1]):  # 后面连着写的版次、状态码：-P01-S4
            tail = parts.pop()
            if STATUS.match(tail):
                out["status"] = out["status"] or tail
            elif REV.match(tail):
                out["rev"] = out["rev"] or tail
            else:  # 图号和图名之间只用了“-”：这是图名的头一个词（…-2302-GROUND FLOOR PLAN）
                words.insert(0, orig[len(parts)])
        no = "-".join(parts)
        if iso_parts(no):
            out["no"], out["kind"] = no, "iso"
        elif len(parts) >= 4 and re.search(r"\d", parts[-1]):
            out["no"], out["kind"] = no, "code"
        if out["no"]:
            title = s[:m.start()] + " " + " ".join(words) + " " + s[m.end():]
    if not out["no"]:
        m = CN_NO.match(s) or SIMPLE_NO.match(s.upper())
        if m:
            out["no"], out["kind"] = f"{m.group(1)}-{m.group(2)}".upper() if m.re is SIMPLE_NO else f"{m.group(1)}-{m.group(2)}", "simple"
            title = s[m.end():]
    m = REV_WORD.search(title)
    if m and m.group(1):
        out["rev"] = out["rev"] or m.group(1).upper()
        title = title[:m.start()] + " " + title[m.end():]
    words = []
    for tok in re.split(r"[\s_\[\]()（）【】]+", title):
        t = tok.strip(" -–—.,，、")
        if not t:
            continue
        up = t.upper()
        m = re.fullmatch(r"(?:第)?([A-Z]{1,2}\d{0,2}|\d{1,2})版", up)
        if m and not out["rev"]:  # “B版”“第2版”
            out["rev"] = m.group(1)
            continue
        if not words and out["no"]:  # 紧跟在图号后面的状态码、版次：_S4_P01_、建施-02 B 一层平面图
            if STATUS.match(up):
                out["status"] = out["status"] or up
                continue
            if REV.match(up) and (re.search(r"\d", up) or len(up) == 1):
                out["rev"] = out["rev"] or up
                continue
        if REV_STRICT.match(up) and not out["rev"]:
            out["rev"] = up
            continue
        words.append(t)
    out["title"] = " ".join(words) or None
    return out


LIST_START = re.compile(r"([一-鿿]{1,3}施|(?<![A-Za-z])[A-Z]{1,3})\s*[-－—_]?\s*(\d{1,4})(?![\d])")
LIST_NEXT = re.compile(r"\s*(~|～|至|到|、|,|，|&|及|和)\s*(?:([一-鿿]{1,3}施|[A-Z]{1,3})\s*[-－—_]?\s*)?(\d{1,4})(?![\d])")


def listed_numbers(stem: str) -> list[str]:
    """合订本文件名里列出的图号：“建施-07~08、10 详图” → 建施-07、建施-08、建施-10。只认同一个前缀的连写。"""
    m = LIST_START.search(stem or "")
    if not m:
        return []
    pre, nums, pos = m.group(1), [int(m.group(2))], m.end()
    width = len(m.group(2))
    while True:
        n = LIST_NEXT.match(stem, pos)
        if not n or (n.group(2) and n.group(2) != pre):
            break
        k = int(n.group(3))
        if n.group(1) in ("~", "～", "至", "到") and nums[-1] < k <= nums[-1] + 50:
            nums += list(range(nums[-1] + 1, k + 1))
        else:
            nums.append(k)
        pos = n.end()
    if len(nums) < 2:
        return []
    return [f"{pre}-{x:0{max(2, width)}d}" for x in dict.fromkeys(nums)]


# 看名字推测图里大概有什么：(图名关键词, 图类, 可能有的内容)；平面图按专业细分
KINDS = [
    (r"目录|drawing\s*(?:register|list|index|schedule)|document\s*register|issue\s*sheet|transmittal", "图纸目录", "图号、图名、版次清单"),
    (r"封面|cover", "封面", "项目名称、设计单位、出图日期"),
    (r"总平面|总图|site\s*(?:plan|layout)|location\s*plan|key\s*plan", "总图", "场地布置、红线、道路、出入口"),
    (r"说明|\bnotes?\b|specification|legend|图例", "设计说明", "设计依据、规范、材料和做法要求"),
    (r"门窗表|door\s*schedule|window\s*schedule", "门窗表", "门窗编号、尺寸、数量"),
    (r"系统|schematic|riser|single\s*line|one\s*line|diagram", "系统图", "系统组成、管线或线路走向、设备"),
    (r"楼梯|stair", "楼梯详图", "楼梯平面、剖面、踏步尺寸"),
    (r"墙身|wall\s*section", "墙身大样", "墙身构造、节点做法"),
    (r"剖面|section", "剖面图", "层高、标高、竖向构造"),
    (r"立面|elevation", "立面图", "外立面、标高、材料"),
    (r"详图|大样|节点|detail", "详图", "节点构造、尺寸、做法"),
    (r"表|schedule|table", "表格", "编号、规格、数量"),
    (r"配筋|reinforcement|rebar", "配筋图", "构件截面、配筋"),
    (r"基础|桩|foundation|pile|footing", "基础图", "基础形式、桩位、承台"),
    (r"吊顶|ceiling|\brcp\b", "吊顶图", "吊顶标高、灯具、风口位置"),
    (r"风管|duct", "风管图", "风管走向、风口、设备"),
    (r"照明|lighting", "照明图", "灯具布置、回路"),
    (r"喷淋|sprinkler|消火栓|hydrant", "消防图", "喷头、消火栓布置"),
    (r"排水|drainage|给水|water|plumbing|sanitary", "给排水图", "管道走向、管径、设备"),
    (r"楼板|板|slab", "楼板图", "板厚、标高、配筋"),
    (r"梁|beam", "梁图", "梁编号、截面、配筋"),
    (r"柱|column", "柱图", "柱编号、截面、定位"),
    (r"平面|布置|\bplans?\b|layout|\bGA\b|general\s*arrangement|setting\s*out", "平面图", None),
]
PLAN_THINGS = {"建筑": "房间布置、轴网、门窗、尺寸", "结构": "构件布置、截面、标高", "暖通": "风管、风口、设备布置",
               "给排水": "管道、管径、设备布置", "电气": "灯具、插座、桥架布置", "机电": "管线和设备布置",
               "景观": "铺装、绿化、标高", "室内": "装修布置、材料", "总图": "场地布置、道路、管线",
               "消防": "喷头、消火栓、报警设备布置", "智能化": "弱电点位、桥架、设备布置", "岩土": "地层、桩位、基坑支护"}


def kind_of(title: str | None, discipline: str | None = None) -> tuple[str | None, str | None]:
    """看图名推测图类和图里可能有的内容；推不出返回 (None, None)。"""
    for rx, kind, things in KINDS:
        if re.search(rx, title or "", re.I):
            return kind, things or PLAN_THINGS.get(discipline or "", "布置、尺寸")
    return None, None


def _squash(text: str) -> str:
    return re.sub(r"[\s_]+", "", text or "").upper()


def guess(paths: list[str], text: str = "") -> dict | None:
    """一个文件（或多页文件里的一页）按名字推：{"no": {"value", "conf", "src"}, "title": …, "rev": …, "discipline": …,
    "level": ISO 楼层代码, "conf": 整体把握}。推不出图号返回 None。

    多页文件（合订本）的文件名说的是整本，图号只从这一页的文字里找。"""
    if not paths:
        return None
    folders, stem, page = split_path(paths[0])
    multi = page is not None
    got = parse_name(stem)
    flat = _squash(text)
    no = conf = src = None
    if got["no"] and not multi:
        no, src = got["no"], "文件名"
        conf = MID if got["kind"] in ("iso", "simple") else LOW
        if flat and _squash(no) in flat:
            conf, src = HIGH, "文件名，PDF 文字里也有"
    if not no and text:  # 文件名里没有（或是合订本）：在这一页的文字里找
        cands = {c.group(1).upper() for c in ISO_NO.finditer(text.upper()) if iso_parts(c.group(1))}
        if len(cands) == 1:
            no, conf, src = cands.pop(), MID, "PDF 文字"
        else:
            m = TEXT_LABEL_NO.search(text)
            if m:
                no, conf, src = m.group(1).strip(" .，,"), LOW, "PDF 文字（图号栏）"
    if not no:
        return None
    title = got["title"] if not multi else None
    tconf, tsrc = (MID if title else LOW), "文件名"
    if title and flat and _squash(title) in flat:
        tconf, tsrc = HIGH, "文件名，PDF 文字里也有"
    disc, dconf, dsrc = None, LOW, ""
    r = role_discipline(no)
    if r:
        disc, dconf, dsrc = r, HIGH, "图号里的角色代码"
    if not disc:
        for prefix, name in CN_PREFIX:
            if no.startswith(prefix):
                disc, dconf, dsrc = name, HIGH, "图号"
                break
    if not disc:
        ip = iso_parts(no)
        disc = words_discipline(ip["originator"]) if ip else None
        if disc:
            dconf, dsrc = MID, "图号里的发起方代码"
    if not disc:
        for f in reversed(folders):
            disc = words_discipline(f)
            if disc:
                dconf, dsrc = MID, f"文件夹“{f}”"
                break
    ip = iso_parts(no)
    return {
        "no": {"value": no, "conf": conf, "src": src},
        "title": {"value": title, "conf": tconf, "src": tsrc} if title else None,
        "rev": {"value": got["rev"], "conf": MID, "src": "文件名"} if got["rev"] and not multi else None,
        "status": got["status"] if not multi else None,
        "discipline": {"value": disc or "其他", "conf": dconf if disc else LOW, "src": dsrc or "认不出"},
        "level": ip["level"] if ip else None,
        "conf": conf,
    }
