"""派生状态：图纸台账、版本合并、批次性质、缺图、关系、事实层、问题清单。

输入只有两类：收到的批次（及其识别结果）和人的决定；另外套用 AI 的智能分类（只影响专业认不出的图和显示时的分组）。
派生结果每次重新计算，因此“截至第 N 批”的状态可以随时还原，差异报告就是相邻两批状态的比较。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field

from . import classify, floors, names, numbering, pdfpages, risk, towers
from .ingest import FOLDER_MARK
from .recognition import HIGH_SCORE, LOW_SCORE, weak_reason
from .store import PLACEHOLDER_EXECUTORS, Project, latest_decisions, now_iso

SAME_TEXT = 0.97      # 文字层相似度阈值：高于它且图形接近，视为重新导出的同一张图
SAME_DHASH = 10       # 差值哈希距离阈值（256 位）
FULL_REISSUE = 0.8    # 图号覆盖率达到它，判断为整套重发
GAP_MAX = 3           # 编号断号：同一百位段里连续缺这么多张以内才提示
COVER_LIST_MAX = 8    # 楼层覆盖：图多于这么多张时只写汇总的楼层范围，不逐张列
# 不分楼层、建哪一层都要看的“通用图”：设计说明，以及没写楼层的柱、剪力墙平面
GENERAL_SHEET = re.compile(r"说明|(?:柱|剪力墙|核心筒).{0,4}(?:平面|布置|定位)")
# 只对这张图有效的值（本层标高、适用楼层）：每张图各写各的，不跨图比较
SHEET_SCOPED = re.compile(r"^(?:本层|本图|本页|该层|此层|本张)|适用(?:楼层|范围)|楼层范围")

STATUS_LABEL = {
    "current": "现行", "new": "本批新增", "updated": "本批升版", "changed": "有变更单",
    "conflict": "冲突", "pending_void": "作废还是漏发待确认", "voided": "已作废", "missing": "缺失",
    "guess": "按文件名暂定",
}
NAMELESS = "文件："  # 文件名里看不出图号的文件，先用文件名代替图号
SEP = " · "  # 多栋楼的项目里，图纸身份写成“塔楼 · 图号”（docs/图纸字段.md 9.1）
CONF_LABEL = {"confirmed": "人工确认", "high": "高置信", "mid": "中置信", "low": "低置信", "conflict": "冲突"}
CONF_RANK = {"confirmed": 4, "high": 3, "mid": 2, "low": 1}
REC_SRC = {"text_layer": "看图识别（PDF 文字层）", "vision": "看图识别", "both": "看图识别（图像和文字层一致）"}
AI_SRC = "AI 推测（智能分类）"  # AI 看名字推的，一律算把握低
# 这次计算用的图号格式（numbering.Numbering，docs/图纸字段.md 9.5）：确认了的才用来定楼栋、去掉版次后缀；没确认的只用来分组显示
_NUMBERING: ContextVar = ContextVar("bidbench_numbering", default=None)

DISCIPLINES = [("建", "建筑"), ("结", "结构"), ("水", "给排水"), ("暖", "暖通"), ("电", "电气"),
               ("A", "建筑"), ("S", "结构"), ("P", "给排水"), ("M", "暖通"), ("N", "暖通"), ("E", "电气"), ("D", "电气")]
FACT_GROUPS = [
    ("工程概况", ("层数", "建筑高度", "屋面标高", "结构形式", "抗震", "面积", "耐火", "防水等级")),
    ("楼层", ("层高",)),
    ("结构构件", ("截面", "墙厚", "混凝土", "钢筋")),
]


# ---------------- 规范化 ----------------

def norm_no(s: str | None) -> str | None:
    """图号规范化：去空格，统一连接符，数字补到两位。建施05 / 建施 -5 / 建施－05 → 建施-05。
    已确认图号格式的项目，套得上格式的图号各段照原样，去掉末尾的版次段（docs/图纸字段.md 9.5）。"""
    if not s:
        return None
    s = re.sub(r"\s+", "", s).replace("－", "-").replace("—", "-").replace("_", "-").upper()
    fmt = _NUMBERING.get()
    if fmt is not None and fmt.confirmed:
        m = fmt.match(s)
        if m:
            return m["base"]
    return _old_norm(s)


def _old_norm(s: str) -> str:
    """没有（或套不上）图号格式时的规范化。"""
    if names.iso_parts(s):  # ISO 19650 编号各段照原样，编号的前导零也留着（PRJ1-STR-ZZ-00-DR-S-0001）
        return s
    m = re.match(r"^(.*?)-?(\d+)([A-Z]?)$", s)
    if not m or not m.group(1):
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}{m.group(3)}"


def no_parts(no: str) -> tuple[str, int | None]:
    m = re.match(r"^(.*)-(\d+)[A-Z]?$", bare(no))
    return (m.group(1), int(m.group(2))) if m else (no, None)


def discipline_of(no: str) -> str:
    no = bare(no)
    fmt = _NUMBERING.get()
    if fmt is not None and fmt.confirmed:  # 已确认的图号格式：专业段按码表
        d = fmt.discipline(no)
        if d:
            return d
    role = names.role_discipline(no)  # ISO 19650 编号里的角色代码：A 建筑、S 结构、M 暖通、E 电气、P 给排水……
    if role:
        return role
    short = no.count("-") <= 2  # 只凭英文首字母认专业只对 A-101 这类短图号；四段以上的图号，第一段是项目代码
    for prefix, name in DISCIPLINES:
        if no.startswith(prefix) and (short or not prefix.isascii()):
            return name
    return "其他"


ISO_REV = {"P": 1, "T": 2, "C": 3}  # ISO 19650 版次：P（初步）在前，T（招标）其次，C（合同、施工）在后


def rev_key(rev: str | None) -> tuple | None:
    """版次排序键：A<B<C，0<1<2；ISO 写法 P01<P02<C01，P01.1 在 P01 和 P02 之间；无法比较返回 None。"""
    if not rev:
        return None
    r = re.sub(r"[版第次\s]", "", rev).upper()
    m = re.fullmatch(r"([PTC])(\d{1,3})(?:\.(\d{1,2}))?", r)
    if m and (len(m.group(2)) >= 2 or m.group(3)):
        return (1000 + ISO_REV[m.group(1)], int(m.group(2)) + int(m.group(3) or 0) / 100)
    m = re.match(r"^([A-Z]*)(\d*)$", r)
    if not m or not r:
        return None
    letters = sum((ord(c) - 64) * 27 ** i for i, c in enumerate(reversed(m.group(1))))
    return (letters, int(m.group(2) or 0))


def _rev_norm(rev: str | None) -> str:
    """版次的统一写法：去掉“第”“版”和空格、转大写；没写版次是“?”。B 和 B版 算同一版。"""
    return re.sub(r"[版第次\s]", "", rev or "").upper() or "?"


def _rev_family(rev: str | None) -> str | None:
    """版次的写法：ISO（P01、C02）、字母（A、B）、数字（0、1、第2版）、字母加数字（A1）。写法不同的版次没法比新旧。"""
    r = _rev_norm(rev)
    if re.fullmatch(r"[PTC]\d{2,3}(?:\.\d{1,2})?", r):
        return "iso"
    return "letter" if re.fullmatch(r"[A-Z]+", r) else "number" if re.fullmatch(r"\d+", r) else (
        "mixed" if re.fullmatch(r"[A-Z]+\d+", r) else None)


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _date_iso(s: str | None) -> str | None:
    """日期整理成 YYYY-MM-DD，比先后用：2026-09-01、2026/9/1、2026年9月1日、12 Sep 2026、12/09/2026（日、月分得清才认）。看不懂返回 None。"""
    t = (s or "").strip()
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(?<!\d)(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})\.?[\s\-/.,]*(\d{2,4})(?!\d)", t)
        if m and m.group(2)[:3].lower() in MONTHS:
            d, mo, y = int(m.group(1)), MONTHS[m.group(2)[:3].lower()], int(m.group(3))
        else:
            m = re.search(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})(?!\d)", t)
            if not m:
                return None
            a, b, y = map(int, m.groups())
            if a > 12 >= b:
                d, mo = a, b
            elif b > 12 >= a:
                d, mo = b, a
            else:
                return None  # 12/09 是 9 月 12 日还是 12 月 9 日分不清
    y += 2000 if y < 100 else 0
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 < y < 2200 else None


def _page_date(p, norm: str) -> dict:
    """这一版的日期：修改记录里这一版那一行的日期优先；没有才用图签上的日期（不少图签上是第一次出图的日期，升版不改）。
    issue 另记图签日期：同一版次几份内容不同、修改记录又没跟着改时，靠它分先后。"""
    tb = p.rec["title_block"]
    f = tb.get("issue_date") or {}
    issue = {"issue": _date_iso(f.get("value")), "issue_conf": conf_of(f.get("confidence")) or "mid",
             "issue_raw": f.get("raw_text") or f.get("value")}
    if norm != "?":
        for row in tb.get("revision_history") or []:
            d = _date_iso(row.get("date"))
            if d and _rev_norm(row.get("revision")) == norm:
                return {"date": d, "src": "修改记录里这一版", "conf": conf_of(row.get("confidence")) or "mid", "raw": row.get("date"),
                        **issue}
    if not issue["issue"]:
        return {"date": None, **issue}
    return {"date": issue["issue"], "src": "图签日期", "conf": issue["issue_conf"], "raw": issue["issue_raw"], **issue}


def _rev_newer(a: dict, b: dict) -> int:
    """a 比 b 新返回 1，旧返回 -1，比不出返回 0：同一种写法比版次；版次写法不同或没写，比日期。"""
    if a["family"] and a["family"] == b["family"] and a["key"] is not None and b["key"] is not None and a["key"] != b["key"]:
        return 1 if a["key"] > b["key"] else -1
    if a["date"] and b["date"] and a["date"] != b["date"]:
        return 1 if a["date"] > b["date"] else -1
    return 0


def _rev_name(r: dict) -> str:
    return "没写版次的那份" if r["norm"] == "?" else f" {r['rev']} 版"


def _why_unclear(a: dict, b: dict) -> str:
    """两版比不出新旧的原因。"""
    names_ = "、".join(x.strip() for x in (_rev_name(a), _rev_name(b)))
    dates = "日期也一样" if a["date"] and a["date"] == b["date"] else "日期也比不出（没写或看不懂）"
    return f"{names_}的版次写法不同或没写，{dates}"


def norm_value(v) -> str:
    s = re.sub(r"\s+", "", str(v)).replace("－", "-").replace("—", "-")
    try:
        return format(float(s), "g")
    except ValueError:
        pass
    s = s.replace("-", "")
    return s[:-2] if s.endswith("结构") else s


def norm_fact_key(name: str) -> str:
    return re.sub(r"\s+", "", name).replace("～", "~")


# ---------------- 字段的统一写法 ----------------

def conf_of(score) -> str | None:
    """看图识别给的 0–1 分数换成把握：0.85 及以上高，0.6 以下低，中间为中。"""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return "high" if score >= HIGH_SCORE else "mid" if score >= LOW_SCORE else "low"


def lower_conf(a: str, b: str) -> str:
    return a if CONF_RANK.get(a, 0) <= CONF_RANK.get(b, 0) else b


def fld(value, src: str, conf: str, raw=None, where: dict | None = None) -> dict | None:
    """一个字段（docs/图纸字段.md 第 3 节）：值、原文（和值不同时才有）、来源、把握（confirmed/high/mid/low）、出处。没有值返回 None。"""
    if value is None or value == "":
        return None
    raw = raw if raw not in (None, "") and str(raw) != str(value) else None
    return {"value": value, "raw": raw, "src": src, "conf": conf, "where": where}


def _where(p) -> dict:
    return {"page_id": p.page_id, "path": p.paths[0]}


def _rec_field(p, key: str, value=None) -> dict | None:
    """看图识别读出的一项：分数换成把握；给了 value 就用整理后的值，识别出的原样写法放进原文。"""
    f = p.rec["title_block"].get(key) or {}
    return fld(f.get("value") if value is None else value, REC_SRC.get(f.get("source"), "看图识别"),
               conf_of(f.get("confidence")) or "mid", f.get("raw_text") or f.get("value"), _where(p))


def _rule_disc_src(no: str) -> tuple[str, str]:
    """按图号认出专业时的来源和把握：ISO 角色代码、中文前缀算高；只凭英文首字母（A、S……）算中。"""
    no = bare(no)
    fmt = _NUMBERING.get()
    if fmt is not None and fmt.confirmed and fmt.discipline(no):
        return "图号里的专业代码（已确认的图号格式）", "high"
    if names.role_discipline(no):
        return "图号里的角色代码", "high"
    return ("图号", "high") if re.match(r"[\u4e00-\u9fff]", no) else ("图号首字母", "mid")


# ---------------- 图纸身份：塔楼 + 图号 ----------------

def bare(key: str) -> str:
    """图纸身份去掉塔楼：“T1 · 建施-01” → “建施-01”。"""
    return key.split(SEP, 1)[1] if key and SEP in key else key


def tower_part(key: str) -> str | None:
    return key.split(SEP, 1)[0] if key and SEP in key else None


def _dec(decisions: dict, kind: str, key: str, old: str | None = None):
    """人工决定：先按现在的身份找；没有再按加塔楼以前的写法（光图号）找，再按确认图号格式以前的写法找
    ——以前做的决定原样带过来。"""
    for k in (key, old, *_legacy(key)):
        if k and (kind, k) in decisions:
            return decisions[(kind, k)]
    return None


def _legacy(key: str) -> list[str]:
    """确认图号格式以前，这张图的身份是怎么写的：前导零去掉（-0002 → -02），多栋楼的项目前面带“塔楼 · ”。"""
    fmt = _NUMBERING.get()
    if fmt is None or not fmt.confirmed or not key:
        return []
    head, sep, tail = key.partition("|")
    n = bare(head)
    if not fmt.match(n):
        return []
    old, t = _old_norm(n), fmt.tower(n)
    cands = [old] + ([f"{t}{SEP}{old}", f"{t}{SEP}{n}"] if t else [])
    return [c + sep + tail for c in dict.fromkeys(cands) if c + sep + tail != key]


def _owns_tower(n: str) -> bool:
    """图号本身就说明了是哪栋（ISO 分区代码，或已确认图号格式里的楼栋段）：身份只写图号，不加塔楼。"""
    if names.iso_parts(n):
        return True
    fmt = _NUMBERING.get()
    m = fmt.match(n) if fmt is not None and fmt.confirmed else None
    return bool(m and "building" in m["parts"])


def _fmt_guess(g: dict | None) -> dict | None:
    """已确认图号格式的项目：文件名图号末尾的版次段拿出来，记成这份文件的版次。"""
    fmt = _NUMBERING.get()
    if not g or fmt is None or not fmt.confirmed:
        return g
    m = fmt.match(g["no"]["value"])
    if m and m["rev"]:
        g["no"]["value"] = m["base"]
        if not g.get("rev"):
            g["rev"] = {"value": m["rev"], "conf": names.MID, "src": "文件名（图号末尾的版次段）"}
    return g


def _type_kind(no: str | None) -> str | None:
    """图号里的类型代码说的图类（详图大样、表格、规格书……），图谱分小框用；确认前也用。"""
    fmt = _NUMBERING.get()
    return fmt.kind(bare(no)) if fmt is not None and no and not no.startswith(NAMELESS) else None


class Keys:
    """图纸身份（docs/图纸字段.md 9.1）：一栋楼的项目就是图号；多栋楼的项目是“塔楼 · 图号”。
    ISO 19650 图号里已经写明分区（或已确认的图号格式里有楼栋段），仍然只用图号。"""

    def __init__(self, pairs):
        self.towers: dict[str, set] = defaultdict(set)  # 图号 → 出现过的塔楼（None：看不出是哪栋）
        for t, n in pairs:
            if n:
                self.towers[n].add(t)
        self.all = sorted({t for ts in self.towers.values() for t in ts if t}, key=towers.order_key)
        self.multi = len(self.all) >= 2

    def real(self, n: str) -> list:
        return sorted((t for t in self.towers.get(n, ()) if t), key=towers.order_key)

    def eff(self, t, n: str):
        """看不出塔楼的文件：这个图号只有一栋楼有，就算那一栋。"""
        if t is None:
            r = self.real(n)
            if len(r) == 1:
                return r[0]
        return t

    def key(self, t, n: str) -> str:
        if not self.multi or _owns_tower(n):
            return n
        t = self.eff(t, n)
        if t is None:
            return f"{towers.UNKNOWN}{SEP}{n}" if len(self.real(n)) >= 2 else n
        return f"{t}{SEP}{n}"

    def resolve(self, n: str, from_t) -> str | None:
        """引用、目录里的图号在这栋楼对应哪张图：同一栋的优先，再是公共的、看不出塔楼的唯一一份；
        只有别的楼才有，返回 None（这栋楼缺）；几栋都有又分不清是哪栋引用的，返回空串。"""
        if n not in self.towers:
            return None
        if not self.multi or _owns_tower(n):
            return n
        ts = self.towers[n]
        if from_t and from_t != towers.COMMON:
            for cand in (from_t, towers.COMMON):
                if cand in ts:
                    return self.key(cand, n)
            return n if ts == {None} else None
        found = {self.key(t, n) for t in ts}
        return found.pop() if len(found) == 1 else ""

    def candidates(self, n: str, from_t) -> list[str]:
        k = self.resolve(n, from_t)
        if k == "":
            return sorted({self.key(t, n) for t in self.towers[n]})
        return [k] if k else []

    def missing_key(self, n: str, from_t) -> str:
        if not self.multi or _owns_tower(n) or not from_t or from_t == towers.COMMON:
            return n
        return f"{from_t}{SEP}{n}"


def _tower_of(paths, text, number, title, sha12, decisions, alias) -> tuple:
    """一个文件是哪栋楼的：(塔楼, 来源, 把握)。人工指定 > ISO 分区代码 > 文件夹 > 文件名、图名 > PDF 文字里的子项。"""
    d = decisions.get(("file_tower", sha12))
    if d and d["value"]:
        return d["value"], f"人工指定（{d['created_at'][:10]}）", "confirmed"

    def a(t):
        return alias.get(t, t)
    fmt = _NUMBERING.get()
    if fmt is not None and fmt.confirmed and number and fmt.match(number):
        t = fmt.tower(number)
        if t:
            return a(t), "图号里的楼栋代码（已确认的图号格式）", "high"
    t = towers.from_iso(number)
    if t:
        return a(t), "图号里的分区代码", "high"
    found: dict[str, str] = {}
    for x in paths or ():
        for f in reversed(names.split_path(x)[0]):
            t = towers.from_folder(f)
            if t:
                found.setdefault(a(t), f)
                break
    if len(found) == 1:
        t, f = next(iter(found.items()))
        return t, f"文件夹“{f}”", "mid"
    if len(found) > 1:
        return towers.COMMON, "同一个文件放在几栋楼的文件夹里（" + "、".join(found) + "）", "mid"
    stem = names.split_path(paths[0])[1] if paths else ""
    for src, txt in (("文件名", stem), ("图名", title or "")):
        t = towers.from_text(txt)
        if t:
            return a(t), src, "mid"
    t = towers.from_pdf_text(text)
    if t:
        return a(t), "PDF 文字（子项）", "mid"
    return None, "", ""


def _tower_field(key: str, info: tuple, keys: "Keys") -> tuple:
    """一张图的塔楼和字段：身份里带的塔楼为准；页面看不出、按“这个图号只有一栋有”算的，把握低。"""
    t, src, conf = info
    tw = tower_part(key)
    if tw is None and not keys.multi:
        tw = t
    elif tw is None:
        tw = keys.eff(t, bare(key))
    if tw == towers.UNKNOWN or tw is None:
        return None, (fld(towers.UNKNOWN, "看不出是哪栋", "low") if keys.multi else None)
    if t == tw:
        return tw, fld(tw, src, conf)
    return tw, fld(tw, f"同一个图号只有 {tw} 有", "low")


def fact_group(name: str, is_standard: bool) -> str:
    if is_standard:
        return "执行规范"
    for g, words in FACT_GROUPS:
        if any(w in name for w in words):
            return g
    return "其他"


# ---------------- 数据装载 ----------------

@dataclass
class PageRec:
    page_id: str
    file_sha: str
    page_index: int
    render_hash: str
    dhash: str
    text: str
    paths: list            # 截至当前批次的所有出处
    batches: list          # 出现过的批次号
    first_batch: int
    rec: dict | None       # 识别结果（自己的或复用像素相同页面的）
    rec_borrowed: bool = False


@dataclass
class Sheet:
    no: str
    title: str | None = None
    discipline: str = "其他"
    pages: list = field(default_factory=list)  # [(rev, PageRec)]


def _load(con, as_of: int):
    pages_rows = con.execute(
        "SELECT p.* FROM pages p WHERE EXISTS (SELECT 1 FROM occurrences o WHERE o.sha256=p.file_sha256 AND o.batch_seq<=?)",
        (as_of,),
    ).fetchall()
    occ = defaultdict(list)
    for o in con.execute("SELECT * FROM occurrences WHERE batch_seq<=? ORDER BY batch_seq, id", (as_of,)):
        occ[o["sha256"]].append(o)
    recs = {r["page_id"]: json.loads(r["data"]) for r in con.execute("SELECT * FROM recognitions")}
    by_hash: dict[str, dict] = {}
    for r in pages_rows:
        rec = recs.get(r["page_id"])
        if rec is None:
            continue
        old = by_hash.get(r["render_hash"])
        if old is None or (old["run"]["executor"] in PLACEHOLDER_EXECUTORS
                           and rec["run"]["executor"] not in PLACEHOLDER_EXECUTORS):
            by_hash[r["render_hash"]] = rec
    out = []
    for r in pages_rows:
        o = occ[r["file_sha256"]]
        rec = recs.get(r["page_id"])
        borrowed = False
        best = by_hash.get(r["render_hash"])
        # 像素相同的页面共用识别结果；有真实执行器的结果时，不用模拟结果
        if best is not None and (rec is None or (rec["run"]["executor"] in PLACEHOLDER_EXECUTORS
                                                 and best["run"]["executor"] not in PLACEHOLDER_EXECUTORS)):
            rec, borrowed = best, best is not recs.get(r["page_id"])
        out.append(PageRec(
            page_id=r["page_id"], file_sha=r["file_sha256"], page_index=r["page_index"],
            render_hash=r["render_hash"], dhash=r["dhash"], text=r["text"],
            paths=[x["path"] + (f"（第 {r['page_index']} 页）" if _multi_page(con, r["file_sha256"]) else "") for x in o],
            batches=sorted({x["batch_seq"] for x in o}), first_batch=min(x["batch_seq"] for x in o),
            rec=rec, rec_borrowed=borrowed,
        ))
    return out


_mp_cache: dict = {}


def _multi_page(con, sha: str) -> bool:
    if sha not in _mp_cache:
        _mp_cache[sha] = (con.execute("SELECT page_count FROM files WHERE sha256=?", (sha,)).fetchone() or [1])[0] > 1
    return _mp_cache[sha]


def _is_archive_name(name: str) -> bool:
    return name.lower().endswith((".zip", ".rar", ".7z", ".tar", ".tgz", ".gz"))


def _in_nested_archive(path: str) -> bool:
    """出处路径形如“批次#3 / 外层.zip / 结构 / 补充图纸.zip / 结施-03.pdf”，中间再出现压缩包即为内层。

    文件夹批次形如“批次#5 / 补充（文件夹） / 结构 / 结构.zip / 结施-06.pdf”：文件夹里直接放的压缩包相当于外层，
    压缩包里再套的压缩包才算内层。
    """
    parts = path.split(" / ")
    if len(parts) > 1 and parts[1].endswith(FOLDER_MARK):
        return sum(_is_archive_name(p) for p in parts[2:-1]) >= 2
    return any(_is_archive_name(p) for p in parts[2:-1])


def _val(rec, key):
    return (rec["title_block"].get(key) or {}).get("value")


def _same_content(a: PageRec, b: PageRec) -> bool:
    if a.render_hash == b.render_hash:
        return True
    return (pdfpages.text_similarity(a.text, b.text) >= SAME_TEXT
            and pdfpages.dhash_distance(a.dhash, b.dhash) <= SAME_DHASH)


# ---------------- 主计算 ----------------

def _assistant_rows(con) -> dict:
    """AI 助手的待确认提议和已确认的项目笔记。它们是原始输入，不参与派生计算，只随状态一起给界面看。"""
    props = []
    for r in con.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY id"):
        props.append({"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"]), "reason": r["reason"],
                      "source": r["source"], "created_at": r["created_at"]})
    notes = [dict(r) for r in con.execute("SELECT * FROM notes ORDER BY id")]
    return {"proposals": props, "notes": notes}


def build(proj: Project, as_of: int | None = None, with_diff: bool = True) -> dict:
    with proj.connect() as con:
        batches = [dict(b) for b in con.execute("SELECT * FROM batches ORDER BY seq")]
        assistant = _assistant_rows(con)
        if not batches:
            return {"project": proj.meta, "generated_at": now_iso(), "as_of": 0, "batches": [], "sheets": [],
                    "edges": [], "issues": [], "facts": {"groups": []}, "baselines": [], "notices": [],
                    "floor_coverage": [], "recognition": {"pages": 0, "recognized": 0, "executors": {}},
                    "security": {"files": [], "scans": [], "pending": 0}, "classification": None, **assistant}
        labels = classify.load_labels(con)
        as_of = as_of or batches[-1]["seq"]
        batches = [b for b in batches if b["seq"] <= as_of]
        decisions = latest_decisions(con)
        pages = _load(con, as_of)
        ext_issues = [dict(r) for r in con.execute(
            "SELECT * FROM extraction_issues WHERE batch_seq<=? ORDER BY id", (as_of,))]
        baselines_rows = [dict(r) for r in con.execute("SELECT * FROM baselines WHERE as_of_batch<=?", (as_of,))]
        file_counts = {r["batch_seq"]: dict(r) for r in con.execute(
            "SELECT o.batch_seq, COUNT(*) AS n, SUM(f.kind='pdf') AS pdf, SUM(o.depth>1) AS nested"
            " FROM occurrences o JOIN files f ON f.sha256=o.sha256 WHERE f.kind!='archive' GROUP BY o.batch_seq")}
        cad: dict[str, list] = defaultdict(list)  # CAD 文件不能看图识别，只按名字暂定
        for r in con.execute("SELECT o.sha256, o.path FROM occurrences o JOIN files f ON f.sha256 = o.sha256"
                             " WHERE f.kind = 'cad' AND o.batch_seq <= ? ORDER BY o.batch_seq, o.id", (as_of,)):
            cad[r["sha256"]].append(r["path"])
        risk_rows = [dict(r) for r in con.execute("SELECT * FROM risk_findings WHERE batch_seq<=? ORDER BY id", (as_of,))]
        risk_rows += risk.blocked_rows(ext_issues)  # 收图时被杀毒软件拦下的文件，也按高风险文件处理
        scans = [dict(r) for r in con.execute("SELECT * FROM av_scans WHERE batch_seq<=? ORDER BY id", (as_of,))]
        risk_paths: dict[str, list] = defaultdict(list)
        for r in con.execute("SELECT o.sha256, o.path FROM occurrences o WHERE o.batch_seq<=? AND o.sha256 IN"
                             " (SELECT sha256 FROM risk_findings) ORDER BY o.batch_seq, o.id", (as_of,)):
            risk_paths[r["sha256"]].append(r["path"])

    state = _compute(proj, batches, pages, decisions, ext_issues, baselines_rows, file_counts, as_of, labels, dict(cad),
                     assistant["proposals"])
    state["classification"] = classify.summary(labels, state)
    state["security"] = _security(risk_rows, scans, risk_paths, decisions, state["issues"])
    state["issues"].sort(key=lambda i: (ISSUE_ORDER[i["level"]], i["kind"], i["title"]))
    state.update(assistant)
    if with_diff and as_of > 1:
        prev = build(proj, as_of - 1, with_diff=False)
        state["diff"] = diff_states(prev, state)
    elif with_diff:
        state["diff"] = diff_states(None, state)
    return state


def _compute(proj, batches, pages, decisions, ext_issues, baselines_rows, file_counts, as_of, labels=None, cad=None,
             proposals=()):
    """先定这次按哪份图号格式拆图号（docs/图纸字段.md 9.5），整个计算都按它。"""
    fmt, info = _numbering(pages, cad or {}, decisions, proposals)
    token = _NUMBERING.set(fmt)
    try:
        st = _compute_state(proj, batches, pages, decisions, ext_issues, baselines_rows, file_counts, as_of, labels, cad)
    finally:
        _NUMBERING.reset(token)
    st["numbering"] = info
    st["issues"] += _numbering_issues(info)
    st["issues"].sort(key=lambda i: (ISSUE_ORDER[i["level"]], i["kind"], i["title"]))
    return st


FMT_SOURCE = {"confirmed": "已确认的图号格式", "proposed": "AI 助手提议的图号格式", "guess": "软件猜的图号格式"}


def _numbering(pages, cad: dict, decisions: dict, proposals) -> tuple:
    """这次用哪份图号格式：人工确认的 > AI 助手待确认的提议 > 软件按文件名猜的（图号不是 ISO 英国附录写法的才猜）。
    返回 (numbering.Numbering 或 None, 给界面看的说明)。选了“先不用”的照旧按 ISO 英国附录的顺序读。"""
    first: dict[str, tuple] = {}  # 每个文件一条：第一页的出处
    for p in pages:
        if p.file_sha not in first or p.page_index < first[p.file_sha][2]:
            first[p.file_sha] = (p.paths[0], p.page_id, p.page_index)
    for sha, cpaths in cad.items():
        first.setdefault(sha, (cpaths[0], None, 0))
    items = numbering.items_from([(path, pid) for path, pid, _i in first.values()])
    need = numbering.need_count(items)
    info: dict = {"status": None, "files": len(items), "need": need}
    d = decisions.get(("numbering", "*"))
    prop = next((x for x in reversed(list(proposals)) if x["kind"] == "numbering"), None)
    if prop:
        info["proposal"] = {"id": prop["id"], "reason": prop["reason"], "created_at": prop["created_at"]}
    fmt = None
    if d and d["value"] == "off":
        info.update(status="off", decided_at=d["created_at"])
        return None, info
    if d:
        try:
            fmt = numbering.Numbering(numbering.validate(json.loads(d["value"])), "confirmed", FMT_SOURCE["confirmed"])
            info["decided_at"] = d["created_at"]
        except (ValueError, TypeError):
            fmt = None
    if fmt is None and prop:
        try:
            fmt = numbering.Numbering(numbering.validate(prop["payload"]["format"]), "proposed", FMT_SOURCE["proposed"])
        except (ValueError, TypeError, KeyError):
            fmt = None
    if fmt is None and need >= numbering.MIN_ASK:
        g = numbering.guess(items)
        fmt = numbering.Numbering(numbering.validate(g), "guess", FMT_SOURCE["guess"]) if g else None
    if fmt is None:
        return None, info
    info.update(status=fmt.status, source=fmt.source, format=fmt.fmt, text=numbering.describe(fmt.fmt),
                coverage=fmt.coverage(items), codes=fmt.codes_seen(items))
    if prop and fmt.status == "confirmed":  # 确认过以后 AI 助手又提议了一份（补充新代码）
        try:
            new = numbering.Numbering(numbering.validate(prop["payload"]["format"]), "proposed")
            info["proposal"].update(format=new.fmt, text=numbering.describe(new.fmt), coverage=new.coverage(items),
                                    codes=new.codes_seen(items))
        except (ValueError, TypeError, KeyError):
            pass
    return fmt, info


def numbering_items(proj: Project) -> list[dict]:
    """文件名里带候选图号的文件（每个文件一条），给 AI 助手的图号汇总用（docs/图纸字段.md 9.5）。"""
    with proj.connect() as con:
        rows = con.execute("SELECT o.sha256, o.path, (SELECT p.page_id FROM pages p WHERE p.file_sha256 = o.sha256"
                           " ORDER BY p.page_index LIMIT 1) AS page_id FROM occurrences o JOIN files f ON f.sha256 = o.sha256"
                           " WHERE f.kind IN ('pdf', 'cad', 'image') ORDER BY o.batch_seq, o.id").fetchall()
    first: dict[str, tuple] = {}
    for r in rows:
        first.setdefault(r["sha256"], (r["path"], r["page_id"]))
    return numbering.items_from(first.values())


def _numbering_issues(info: dict) -> list[dict]:
    """问题中心里的“图号格式”：还没确认时请人确认；确认后有不少图号套不上、或有没写意思的代码时提醒。"""
    if info.get("status") in ("guess", "proposed") and info["need"] >= numbering.MIN_ASK:
        cov = info["coverage"]
        who = "AI 助手提议" if info["status"] == "proposed" else "软件按文件名猜"
        opts = [] if info["status"] == "proposed" else [
            {"label": "按这个格式确认", "kind": "numbering", "key": "*",
             "value": json.dumps(info["format"], ensure_ascii=False, sort_keys=True)}]
        opts.append({"label": "先不用", "kind": "numbering", "key": "*", "value": "off"})
        return [_issue("warn", "图号格式", "numbering", "图号格式待确认",
                       f"{info['need']} 个文件的图号不是 ISO 英国附录的写法。{who}的格式是“{info['text']}”，"
                       f"套得上 {cov['matched']}/{cov['total']} 个文件。确认后用来分楼栋、认专业和楼层，"
                       "带版次后缀的文件并到同一张图；确认之前只用来分组显示。"
                       + ("可以让 AI 助手认一遍：看不懂的代码它会先看图名、目录页和图签文字。" if info["status"] == "guess" else
                          "在上方“图号格式”里确认或驳回。"), {}, opts)]
    if info.get("status") == "confirmed":
        cov = info["coverage"]
        unknown = {r: v for r, v in cov["unknown_codes"].items() if r in ("discipline", "type")}
        if cov["unmatched"] >= 5 or unknown:
            what = (f"{cov['unmatched']} 个文件的图号套不上（如 {'、'.join(cov['unmatched_examples'][:3])}）"
                    if cov["unmatched"] >= 5 else "")
            codes = "；".join(f"{numbering.ROLES[r]}代码 {'、'.join(v[:6])} 没写意思" for r, v in unknown.items())
            title = "有图号套不上已确认的图号格式" if cov["unmatched"] >= 5 else "图号格式里有代码没写意思"
            return [_issue("info", "图号格式", "numbering_new", title,
                           "；".join(x for x in (what, codes) if x) + "。可以让 AI 助手补充，补完再确认一次。", {}, [])]
    return []


def _compute_state(proj, batches, pages, decisions, ext_issues, baselines_rows, file_counts, as_of, labels=None, cad=None):
    labels = labels or {"sheet": {}, "fact": {}, "last": None}

    def lab(no: str):  # 智能分类按身份记；加塔楼以前按光图号记的照样用
        return labels["sheet"].get(no) or labels["sheet"].get(bare(no))

    def dec(kind: str, key: str, suffix: str = ""):
        return _dec(decisions, kind, key + suffix, bare(key) + suffix)

    def disc_how(no: str, paths=()) -> tuple[str, str, str]:
        """专业及其来源、把握：按图号认；认不出的用智能分类给的（AI 推测，低）；再认不出看所在的文件夹（中）。"""
        rule = discipline_of(no)
        if rule != "其他":
            return (rule, *_rule_disc_src(no))
        fmt = _NUMBERING.get()
        fd = fmt.discipline(bare(no)) if fmt is not None and not fmt.confirmed else None
        if fd:  # 还没确认的图号格式：只在认不出专业时用，把握低
            return fd, f"图号里的专业代码（{fmt.source}，待确认）", "low"
        ai = classify.sheet_discipline(lab(no), rule)
        if ai != "其他":
            return ai, AI_SRC, "low"
        for x in paths:
            for f in reversed(names.split_path(x)[0]):
                w = names.words_discipline(f)
                if w:
                    return w, f"文件夹“{f}”", "mid"
        return "其他", "认不出", "low"

    def disc(no: str, paths=()) -> str:
        return disc_how(no, paths)[0]

    # ---- 塔楼：每个文件先认出是哪一栋，图纸身份是“塔楼 + 图号” ----
    alias = {k: d["value"] for (kind, k), d in decisions.items() if kind == "tower_alias" and d["value"]}
    ptower: dict[str, tuple] = {}  # 页面 → (塔楼, 来源, 把握)
    pnum: dict[str, str | None] = {}  # 页面 → 图号（看图识别读出的，没读出就按文件名）
    pairs = []
    for p in pages:
        n = None
        if p.rec is not None and p.rec["page_kind"] in ("变更单", "答疑或澄清"):
            pass
        elif p.rec is not None and norm_no(_val(p.rec, "drawing_no")):
            n = norm_no(_val(p.rec, "drawing_no"))
        else:
            g = _page_guess(p)
            n = norm_no(g["no"]["value"]) if g else None
        pnum[p.page_id] = n
        ptower[p.page_id] = _tower_of(p.paths, p.text, n, _val(p.rec, "drawing_title") if p.rec else None,
                                      p.file_sha[:12], decisions, alias)
        if n:
            pairs.append((ptower[p.page_id][0], n))
    path_tower: dict[str, tuple] = {}  # CAD 文件第一个出处 → (塔楼, 来源, 把握)
    for sha, cpaths in (cad or {}).items():
        g = _fmt_guess(names.guess(cpaths, ""))
        n = norm_no(g["no"]["value"]) if g else None
        path_tower[cpaths[0]] = _tower_of(cpaths, "", n, None, sha[:12], decisions, alias)
        if n:
            pairs.append((path_tower[cpaths[0]][0], n))
    keys = Keys(pairs)
    sheet_tower: dict[str, str | None] = {}  # 图纸身份 → 塔楼

    sheets: dict[str, Sheet] = {}
    notices = []  # 变更单、答疑等非图纸文件
    unrecognized = []
    unnumbered = []  # 看图识别了、但没读出图号的页面：也按文件名暂定
    for p in pages:
        if p.rec is None:
            unrecognized.append(p)
            continue
        kind = p.rec["page_kind"]
        no = norm_no(_val(p.rec, "drawing_no"))
        if kind in ("变更单", "答疑或澄清"):
            notices.append(p)
            continue
        if not no:
            unnumbered.append(p)
            continue
        key = keys.key(ptower[p.page_id][0], no)
        s = sheets.setdefault(key, Sheet(no=key, discipline=disc(no)))
        s.pages.append((_val(p.rec, "revision"), p))

    issues: list[dict] = []
    current_page: dict[str, PageRec] = {}
    sheet_out: dict[str, dict] = {}

    # ---- 版本链与现行版（docs/图纸字段.md 9.2）：先比版次；版次一样、写法不同或没写，再比日期；收图先后不算 ----
    for no, s in sorted(sheets.items()):
        by_rev: dict[str, list[PageRec]] = defaultdict(list)
        raw_rev: dict[str, str] = {}
        for rev, p in s.pages:
            n = _rev_norm(rev)
            by_rev[n].append(p)
            raw_rev.setdefault(n, rev or "?")
        revisions = []
        for n, plist in by_rev.items():
            groups: list[dict] = []  # 同一版次里内容相同的文件算一份
            for p in sorted(plist, key=lambda x: x.first_batch):
                g = next((g for g in groups if _same_content(g["pages"][0], p)), None)
                if g is None:
                    groups.append({"pages": [p], **_page_date(p, n)})
                else:
                    g["pages"].append(p)
                    if not g.get("date"):
                        g.update(_page_date(p, n))
            revisions.append({"rev": raw_rev[n], "norm": n, "key": rev_key(n) if n != "?" else None,
                              "family": _rev_family(n), "date": max((g["date"] for g in groups if g.get("date")), default=None),
                              "first_batch": min(p.first_batch for p in plist), "groups": groups})
        # 没写版次的排前面：比不出新旧时，默认用写了版次的那份
        revisions.sort(key=lambda r: (r["key"] is not None, r["key"] or (0, 0), r["date"] or "", r["first_batch"]))
        best = revisions[0]
        for r in revisions[1:]:
            if _rev_newer(r, best) >= 0:
                best = r
        unclear = [r for r in revisions if r is not best and _rev_newer(best, r) <= 0]  # 比不出新旧
        override = dec("sheet_current", no)
        chosen_rev = override and next((r for r in revisions if r["norm"] == _rev_norm(override["value"])), None)
        cur = chosen_rev or best
        status, notes = "current", []

        # 同一版次几份内容不同：日期晚的为现行，提醒“改了图没升版”；日期也分不出先后，问人
        groups = cur["groups"]
        pick = dec("sheet_pick", no, f"|{cur['rev']}")
        hit = pick and next((g for g in groups if any(p.page_id == pick["value"] for p in g["pages"])), None)
        same_unclear = False
        if hit:
            chosen = hit
            if len(groups) > 1:
                notes.append(f"{cur['rev']} 版存在内容不同的多个文件，已按人工决定采用其中一份。")
        elif len(groups) > 1:
            gkey = lambda g: (g.get("date") or "", g.get("issue") or "")  # noqa: E731  修改记录日期一样时再比图签日期
            latest = max(groups, key=gkey)
            if any(gkey(latest)) and sum(1 for g in groups if gkey(g) == gkey(latest)) == 1:
                chosen = latest
            else:
                chosen, same_unclear = groups[-1], True  # 先按最后收到的那份
        else:
            chosen = groups[0]
        cp = chosen["pages"][0]
        current_page[no] = cp
        title = _val(cp.rec, "drawing_title")
        s.title = title
        d, dsrc, dconf = disc_how(no, cp.paths)
        s.discipline = d
        batches_desc = "、".join(f"批次#{g['pages'][0].first_batch}" for g in groups)
        if same_unclear:
            status = "conflict"
            issues.append(_issue("crit", "版本冲突", f"rev_dup:{no}", f"{no} {title or ''}".strip(),
                                 f"{batches_desc} 的 {no} 都是{_rev_name(cur)}，但内容不同，日期也分不出先后，可能改图未升版。",
                                 {"sheet": no},
                                 [{"label": f"采用批次#{g['pages'][0].first_batch} 的文件", "kind": "sheet_pick",
                                   "key": f"{no}|{cur['rev']}", "value": g["pages"][0].page_id} for g in groups]))
        elif len(groups) > 1 and not hit and cur["norm"] != "?":
            ck = f"{no}|{cur['norm']}"
            dd = dec("rev_check", no, f"|{cur['norm']}")
            if not (dd and dd["value"] == "ignore"):
                when = (chosen["date"] if all(g.get("date") != chosen["date"] for g in groups if g is not chosen)
                        else f"图签日期 {chosen['issue']}")
                iss = _issue("warn", "改图未升版", f"rev_same:{no}", f"{no} 改了图没升版",
                             f"{batches_desc} 的 {no} 都是 {cur['rev']} 版，但内容不同。按日期以 {when} 的那份"
                             f"（批次#{cp.first_batch}）为现行；投标时可以列入答疑，请发图方确认。", {"sheet": no},
                             [{"label": f"改用批次#{g['pages'][0].first_batch}（{g.get('issue') or g.get('date') or '没写日期'}）的文件",
                               "kind": "sheet_pick", "key": f"{no}|{cur['rev']}", "value": g["pages"][0].page_id}
                              for g in groups if g is not chosen]
                             + [{"label": "列入答疑", "kind": "rev_check", "key": ck, "value": "ask"},
                                {"label": "知道了，以日期晚的为准", "kind": "rev_check", "key": ck, "value": "ignore"}],
                             confirmed=bool(dd and dd["value"] == "ask"))
                iss["qa"] = (f"{no} {title or ''} 有几份都标为 {cur['rev']} 版、内容却不同的文件（{batches_desc}），"
                             "请确认以哪一份为准，或升版后重发。")
                issues.append(iss)
        if not chosen_rev:  # 人工定过现行版的，不再问
            if unclear:
                status = "conflict"
                issues.append(_issue("crit", "版本冲突", f"rev_unclear:{no}", f"{no} 哪一版是现行版拿不准",
                                     "；".join(_why_unclear(best, r) for r in unclear) + f"。先暂按{_rev_name(best)}算，请指定现行版。",
                                     {"sheet": no},
                                     [{"label": f"以{_rev_name(x)}为现行", "kind": "sheet_current", "key": no, "value": x["rev"]}
                                      for x in [best, *unclear]]))
            for b in revisions:  # 版次高的日期反而早
                if (b is not cur and cur["family"] and cur["family"] == b["family"] and cur["key"] is not None
                        and b["key"] is not None and cur["key"] > b["key"] and cur["date"] and b["date"] and cur["date"] < b["date"]):
                    status = "conflict"
                    issues.append(_issue("crit", "版本冲突", f"rev_date:{no}:{b['norm']}", f"{no} 版次和日期对不上",
                                         f"{cur['rev']} 版的日期 {cur['date']} 比 {b['rev']} 版的 {b['date']} 还早。先按版次以 {cur['rev']} 版为现行；"
                                         f"也可能是日期写错了，或者 {cur['rev']} 版已经作废。", {"sheet": no},
                                         [{"label": f"以 {x['rev']} 版为现行", "kind": "sheet_current", "key": no, "value": x["rev"]}
                                          for x in (cur, b)]))
            for r in revisions:  # 后收到的反而是旧版：按版次定，只提醒核实
                if r is not cur and r["first_batch"] > cur["first_batch"] and _rev_newer(cur, r) > 0:
                    issues.append(_issue("warn", "版本提醒", f"rev_older:{no}:{r['norm']}", f"{no} 后收到的反而是旧版",
                                         f"批次#{r['first_batch']} 收到的是{_rev_name(r)}，比现行的{_rev_name(cur)}旧。"
                                         f"按版次以{_rev_name(cur)}为现行；请核实是不是{_rev_name(cur)}作废了、退回了旧版。", {"sheet": no},
                                         [{"label": f"确认以{_rev_name(x)}为现行", "kind": "sheet_current", "key": no, "value": x["rev"]}
                                          for x in (cur, r)]))
        if status == "current":
            cb = min(p.first_batch for p in chosen["pages"])
            if cb == as_of and (len(revisions) > 1 or len(groups) > 1):
                status = "updated"
            elif cb == as_of and as_of > 1 and min(r["first_batch"] for r in revisions) == as_of:
                status = "new"
        vlist = []
        for r in revisions:
            tie = len({g.get("date") for g in r["groups"]}) < len(r["groups"])  # 同版次几份的修改记录日期一样
            for g in r["groups"]:
                if r is cur:
                    state_txt = "现行" if g is chosen else "内容不同，待确认" if same_unclear else "同版次另一份，未采用"
                else:
                    state_txt = "待确认" if not chosen_rev and r in unclear else "已替代"
                vlist.append({"rev": r["rev"], "date": (g.get("issue") or g.get("date")) if tie else g.get("date"),
                              "batch": g["pages"][0].first_batch, "state": state_txt,
                              "page_id": g["pages"][0].page_id, "paths": sorted({x for p in g["pages"] for x in p.paths})})
        cls = classify.sheet_view(lab(no), discipline_of(no))
        tw, tower_f = _tower_field(no, ptower[cp.page_id], keys)
        sheet_tower[no] = tw
        fields = {"no": _rec_field(cp, "drawing_no", bare(no)), "tower": tower_f, "title": _rec_field(cp, "drawing_title"),
                  "rev": _rec_field(cp, "revision"),
                  "date": fld(chosen["issue"], "看图识别（图签日期）", chosen["issue_conf"], chosen.get("issue_raw"), _where(cp))
                  if len({g.get("date") for g in groups}) < len(groups) and chosen.get("issue")
                  else fld(chosen["date"], f"看图识别（{chosen['src']}）", chosen["conf"], chosen.get("raw"), _where(cp))
                  if chosen.get("date") else _rec_field(cp, "issue_date"),
                  "discipline": fld(d, dsrc, dconf, where=None if dsrc == AI_SRC else _where(cp)),
                  "content": fld(cp.rec["content"]["summary"], "看图识别", "mid", where=_where(cp))
                  or fld((cls or {}).get("content"), AI_SRC, "low")}
        if override and cur["rev"] == override["value"]:  # 人工定的现行版
            fields["rev"] = fld(cur["rev"], f"人工确认（{override['created_at'][:10]}）", "confirmed", where=_where(cp))
        sheet_out[no] = {
            "no": no, "number": bare(no), "tower": tw, "title": title, "discipline": s.discipline, "status": status,
            "current_rev": cur["rev"], "current_page": cp.page_id, "versions": vlist,
            "paths": cp.paths, "inner": all(_in_nested_archive(x) for x in cp.paths),
            "notes": notes, "borrowed": cp.rec_borrowed, "own_values": [],
            "rec_notes": [{"type": x["type"], "text": x["description"]} for x in cp.rec["issues"]],
            "cls": cls, "fields": fields, "type_kind": _type_kind(no),
        }

    # ---- 变更单 ----
    notice_out = []
    for p in notices:
        ft = ptower[p.page_id][0]
        affected = sorted({k for r in p.rec["references"] if norm_no(r["target_drawing_no"])
                           for k in (keys.candidates(norm_no(r["target_drawing_no"]), ft)
                                     or [keys.missing_key(norm_no(r["target_drawing_no"]), ft)])})
        nid = re.sub(r"\s+", "", _val(p.rec, "drawing_no") or "") or p.page_id
        notice_out.append({"id": nid, "title": _val(p.rec, "drawing_title"), "kind": p.rec["page_kind"],
                           "page_id": p.page_id, "batch": p.first_batch, "affects": affected, "paths": p.paths})
        for no in affected:
            if no in sheet_out:
                sheet_out[no]["notes"].append(f"{nid}（批次#{p.first_batch}）修改了此图的局部，相关内容以变更文件为准。")
                if sheet_out[no]["status"] in ("current", "updated", "new"):
                    sheet_out[no]["status"] = "changed"

    # ---- 批次性质 ----
    known_before: set[str] = set()
    batch_out = []
    sheets_in_batch = defaultdict(set)
    for no, s in sheets.items():
        for _, p in s.pages:
            for b in p.batches:
                sheets_in_batch[b].add(no)
    catalog_in_batch = defaultdict(bool)
    for s in sheets.values():
        for _, p in s.pages:
            if p.rec["page_kind"] == "图纸目录":
                for b in p.batches:
                    catalog_in_batch[b] = True
    notice_batches = {n["batch"] for n in notice_out}
    pending_void: dict[str, int] = {}
    for b in batches:
        seq = b["seq"]
        here = sheets_in_batch[seq]
        kinds, conf = [], "中"
        if seq == 1 or not known_before:
            kinds, conf = ["首次发图"], "高"
        else:
            cover = len(here & known_before) / max(len(known_before), 1)
            name = b["name"] + b["archive_name"]
            if cover >= FULL_REISSUE or (catalog_in_batch[seq] and cover >= 0.5):
                kinds, conf = ["整套重发"], "高" if cover >= FULL_REISSUE else "中"
                for no in sorted(known_before - here):
                    pending_void[no] = seq
            elif here & known_before:
                kinds.append("部分修改")
            if here - known_before and "整套重发" not in kinds:
                kinds.append("补充图纸")
            if seq in notice_batches:
                kinds.append("变更单")
            if re.search(r"作废", name):
                kinds.append("作废通知")
            if not kinds:
                kinds, conf = ["无图纸"], "低"
        for no in here:
            pending_void.pop(no, None)  # 后来又出现了
        known_before |= here
        fc = file_counts.get(seq, {})
        batch_out.append({
            "seq": seq, "name": b["name"], "received_at": b["received_at"], "archive": b["archive_name"],
            "kinds": kinds, "confidence": conf, "sheets": len(here),
            "files": fc.get("n", 0), "pdf": fc.get("pdf", 0) or 0,
            "issues": [i for i in ext_issues if i["batch_seq"] == seq],
        })

    for no, seq in pending_void.items():
        d = dec("sheet_void", no)
        if d and d["value"] == "voided":
            sheet_out[no]["status"] = "voided"
            continue
        if d and d["value"] == "keep":
            continue
        sheet_out[no]["status"] = "pending_void"
        issues.append(_issue("warn", "作废还是漏发", f"void:{no}", f"{no} {sheet_out[no]['title'] or ''}".strip(),
                             f"批次#{seq} 判断为整套重发，但其中没有这张图。是作废了，还是漏发了？",
                             {"sheet": no},
                             [{"label": "确认已作废", "kind": "sheet_void", "key": no, "value": "voided"},
                              {"label": "仍然有效（漏发）", "kind": "sheet_void", "key": no, "value": "keep"}]))

    live = {no for no, s in sheet_out.items() if s["status"] != "voided"}

    # ---- 一级识别：还没看图识别的页面、CAD 文件，按文件名暂定成图纸 ----
    prov, nameless = _first_guess(unrecognized + unnumbered, cad or {}, sheet_out, labels, disc_how, keys, ptower, path_tower)
    for k, v in prov.items():
        sheet_tower[k] = v["tower"]

    # ---- 每张图管哪几层（图纸卡片上的同层图纸、楼层覆盖都用）：原叫法 + 换算出的第几层 ----
    bases = _floor_bases(list(sheet_out.values()) + list(prov.values()), sheet_tower, decisions, keys.multi, issues)

    def base_for(t):
        return bases.get(t) or bases["*"]
    for no, s in sheet_out.items():
        b, sure = base_for(sheet_tower.get(no))
        fl, how, src_text = _sheet_floors(s["title"], current_page[no], b)
        raw = floors.floor_names(src_text)
        conf = (s["fields"]["title"] or {}).get("conf", "mid") if how == "图名" else "mid"
        if not fl:  # 图名、说明里看不出：用图号里的楼层代码
            fl, how, raw, conf = _code_floors(bare(no), b, sure)
        _set_floors(s, fl, how, raw)
        english = how == "图名" and floors.LEVEL_WORD.search(s["title"] or "")
        if english and not sure:
            conf = lower_conf(conf, "mid")  # Level 01 是 1F 还是 2F，是猜的
        src = (how if how.startswith("图号") else f"{how}（看图识别）") \
            + (f"，{BASE_SHORT[b]}（{'已确认' if sure else '猜的'}）" if english else "")
        s["fields"]["floors"] = fld(s["floors_name"], src, conf, where=_where(current_page[no])) if fl else None
    for no, s in prov.items():
        b, sure = base_for(s["tower"])
        fl = floors.parse_floors(s["title"] or "", b)
        english = fl and floors.LEVEL_WORD.search(s["title"] or "")
        how, raw = ("图名（按文件名）" + (f"，{BASE_SHORT[b]}（{'已确认' if sure else '猜的'}）" if english else ""),
                    floors.floor_names(s["title"] or "")) if fl else ("", [])
        cconf = names.LOW
        if not fl:
            fl, how, raw, cconf = _code_floors(bare(no), b, sure)
        _set_floors(s, fl, how, raw)
        where = {"page_id": s["current_page"], "path": s["paths"][0]}
        s["fields"]["floors"] = fld(s["floors_name"], how, cconf, where=where) if fl else None
        if (s["cls"] or {}).get("content"):  # 智能分类看名字推测的内容优先
            s["fields"]["content"] = fld(s["cls"]["content"], AI_SRC, names.LOW)
            continue
        kind, things = names.kind_of(s["title"], s["discipline"])
        if kind:
            what = f"{s['floors_text'] + ' ' if s['floors_text'] and len(s['floors']) <= 3 else ''}{s['discipline'] if s['discipline'] != '其他' else ''}{kind}"
            before = s["fields"].get("content")  # 合订本的说明留着，接在前面
            s["fields"]["content"] = fld((before["value"] + "；" if before else "") + f"{what}：可能有{things}",
                                         "看名字推测", names.LOW, where=where)

    # ---- 关系 ----
    edges, dangling = [], defaultdict(list)
    for no, p in current_page.items():
        if no not in live:
            continue
        for r in p.rec["references"]:
            n = norm_no(r["target_drawing_no"])
            t = keys.resolve(n, sheet_tower.get(no)) if n else ""
            if t == "":  # 没写图号，或几栋楼都有、分不清引用的是哪栋的
                continue
            t = t or keys.missing_key(n, sheet_tower.get(no))
            if t == no:
                continue
            etype = "cross" if disc(t) != disc(no) else "ref"
            edges.append({"from": no, "to": t, "type": etype, "label": r["raw_text"], "bbox": r["bbox"],
                          "page_id": p.page_id})
            if t not in live:
                dangling[t].append(no)

    # ---- 缺图 ----
    listed: dict[str, dict] = {}
    for no, p in current_page.items():
        if p.rec["page_kind"] == "图纸目录" and no in live:
            for e in p.rec["catalog_entries"]:
                n = norm_no(e["drawing_no"])
                t = keys.resolve(n, sheet_tower.get(no)) if n else ""
                if t != "":
                    listed[t or keys.missing_key(n, sheet_tower.get(no))] = {"catalog": no, "title": e["drawing_title"],
                                                                             "rev": e["revision"]}
    missing: dict[str, dict] = {}
    for t, info in listed.items():
        if t not in sheet_out:
            missing.setdefault(t, {"no": t, "title": info["title"], "evidence": [], "level": "高", "related": set()})
            missing[t]["evidence"].append(f"目录 {info['catalog']} 列出")
            missing[t]["related"].add(info["catalog"])
            edges.append({"from": info["catalog"], "to": t, "type": "catalog", "label": "目录列出", "bbox": None,
                          "page_id": current_page[info["catalog"]].page_id})
    for t, froms in dangling.items():
        if t in sheet_out:
            continue  # 已作废的图仍被引用：在下面单独提示
        missing.setdefault(t, {"no": t, "title": None, "evidence": [], "level": "高", "related": set()})
        missing[t]["evidence"].append("被 " + "、".join(sorted(set(froms))) + " 引用")
        missing[t]["related"] |= set(froms)
    for t, froms in dangling.items():
        if t in sheet_out and sheet_out[t]["status"] == "voided":
            issues.append(_issue("warn", "引用已作废图纸", f"ref_void:{t}", f"{t} 已作废但仍被引用",
                                 "引用它的图纸：" + "、".join(sorted(set(froms))), {"sheet": t}, [], sheets=froms))
    # 编号断号：每栋楼各查各的
    by_prefix = defaultdict(set)
    for no in list(sheet_out) + list(listed):
        pre, n = no_parts(no)
        if n is not None:
            by_prefix[(sheet_tower[no] if no in sheet_out else tower_part(no), pre)].add(n)
    for (tw, pre), nums in by_prefix.items():
        if len(nums) < 3:
            continue
        ordered = sorted(nums)
        for a, b in zip(ordered, ordered[1:]):
            # 只在同一百位段里、连续缺几张时才算断号；更大的空档多半是分段编号（1xx 平面、2xx 剖面、9xx 系统图）
            if not (1 < b - a <= GAP_MAX + 1 and a // 100 == b // 100):
                continue
            for n in range(a + 1, b):
                t = f"{pre}-{n:02d}"
                if keys.resolve(t, tw):  # 公共的、或看不出塔楼的那份就是
                    continue
                t = keys.missing_key(t, tw)
                if t not in missing:
                    missing[t] = {"no": t, "title": None, "evidence": [f"{pre} 编号断号"], "level": "中", "gap": True}
    # 还没识别的页面：文件名里就有这个图号的，多半只是还没识别到，先不算缺图（识别途中、只识别了前几档时常见）
    pending_names = "\n".join(sorted({x.split(" / ")[-1] for p in unrecognized for x in p.paths}))
    pending: list[tuple[str, str]] = []
    for t, m in sorted(missing.items()):
        d = dec("missing", t)
        if d and d["value"] == "not_needed":
            continue
        if t in prov and not prov[t]["pages"]:
            continue  # 只有 CAD 文件：图是有的，只是不能看图识别
        hit = _in_pending(t, pending_names) or (prov[t]["paths"][0].split(" / ")[-1] if t in prov else None)
        if hit:
            pending.append((t, hit))
            continue
        if d and d["value"] == "ask":
            m["confirmed"] = True
        m["status"] = "missing"
        desc = "；".join(m["evidence"]) + "，但截至目前所有批次中都没有。"
        issues.append(_issue("crit" if m["level"] == "高" else "warn", "缺图",
                             f"missing:{t}", f"{t} {m['title'] or ''}".strip(), desc, {"sheet": t},
                             [{"label": "列入答疑，向招标方要图", "kind": "missing", "key": t, "value": "ask"},
                              {"label": "确认不需要（作废或笔误）", "kind": "missing", "key": t, "value": "not_needed"}],
                             level_word=m["level"], confirmed=bool(m.get("confirmed")), sheets=m.get("related")))
    missing = {t: m for t, m in missing.items() if m.get("status") == "missing"}
    if pending:
        eg = "；".join(f"{t}（{name}）" for t, name in pending[:5]) + ("……" if len(pending) > 5 else "")
        issues.append(_issue("info", "待识别", "pending_refs", f"{len(pending)} 个被引用或目录列出的图号还没识别",
                             f"它们在还没识别的页面的文件名里：{eg}。识别完再判断是否缺图。", {}, []))

    # ---- 目录版次 ----
    _catalog_revisions(sheet_out, current_page, decisions, issues, live,
                       lambda n, cat: keys.resolve(n, sheet_tower.get(cat)))

    # ---- 解包问题 ----
    for i in ext_issues:
        if i["kind"] == "blocked":
            continue  # 被杀毒软件拦下的文件在“风险文件”里处理
        label = {"encrypted": "加密", "corrupt": "损坏", "no_tool": "缺少解压工具", "volume": "分卷",
                 "too_deep": "嵌套过深", "too_large": "超出上限", "symlink": "符号链接",
                 "unreadable": "读不出来"}.get(i["kind"], i["kind"])
        if i["kind"] == "symlink":
            issues.append(_issue("info", "解包问题", f"extract:{i['id']}", f"{label}：{i['path'].split(' / ')[-1]}",
                                 f"{i['detail']}。位置：{i['path']}。", {"batch": i["batch_seq"]}, []))
            continue
        issues.append(_issue("warn", "解包问题", f"extract:{i['id']}", f"{label}：{i['path'].split(' / ')[-1]}",
                             f"{i['detail'].rstrip('。')}。位置：{i['path']}。这里面可能藏着缺失的图纸。", {"batch": i["batch_seq"]}, []))

    # ---- 塔楼：几栋都有的图号看不出是哪栋；不同叫法可能是同一栋 ----
    asked = set()
    for p in pages:
        n = pnum.get(p.page_id)
        sha = p.file_sha[:12]
        if ptower[p.page_id][0] is not None or not n or sha in asked or len(keys.real(n)) < 2:
            continue
        asked.add(sha)
        fname, k = p.paths[0].split(" / ")[-1], keys.key(None, n)
        issues.append(_issue("warn", "塔楼不明", f"tower:{sha}", f"{fname} 是哪栋楼的？",
                             f"图号 {n} 在 {'、'.join(keys.real(n))} 都有，这份文件的文件夹、文件名和图上都看不出是哪栋，"
                             f"先单列为“{k}”，不跟哪栋的版本比。", {"sheet": k},
                             [{"label": t, "kind": "file_tower", "key": sha, "value": t} for t in keys.real(n) if t != towers.COMMON]
                             + [{"label": "几栋共用", "kind": "file_tower", "key": sha, "value": towers.COMMON}]))
    for a, b in towers.alias_pairs(keys.all):
        if ("tower_alias", b) in decisions:
            continue
        issues.append(_issue("warn", "塔楼叫法", f"tower_alias:{b}", f"{a} 和 {b} 是同一栋楼吗？",
                             f"文件夹或图上有的写 {a}，有的写 {b}。是同一栋就并成一个，缺图、版本按一栋算；不是就分开算。", {},
                             [{"label": f"是同一栋，统一叫 {a}", "kind": "tower_alias", "key": b, "value": a},
                              {"label": "不是同一栋", "kind": "tower_alias", "key": b, "value": ""}]))

    # ---- 未识别页面 ----
    if unrecognized:
        named = sum(1 for e in prov.values() if not e["no"].startswith(NAMELESS))
        issues.append(_issue("info", "待识别", "unrecognized", f"{len(unrecognized)} 页还没看图识别",
                             f"已按文件名暂定成 {named} 张图" + (f"，另有 {nameless} 个文件看不出图号、先用文件名代替" if nameless else "")
                             + "（图谱里的虚线框）。这些信息把握不高，不拿来判断缺图和版本；"
                             "在图谱里点一张图或一个框可以按需看图识别，也可以点上方的「识别」按优先级识别。", {}, []))
    newer = [(no, f) for no, s in sheet_out.items() for f in s.get("pending_files", []) if f.get("newer")]
    if newer:
        eg = "；".join(f"{no} 文件名 {f['rev']} 版（{f['path'].split(' / ')[-1]}）" for no, f in newer[:5])
        issues.append(_issue("info", "待识别", "newer_files", f"{len(newer)} 个文件的文件名版次比现行版新，还没看图识别",
                             f"{eg}{'……' if len(newer) > 5 else ''}。看图识别后再确认现行版，在这之前别拿旧版建模。", {}, [],
                             sheets=[no for no, _f in newer]))
    _name_checks(sheet_out, current_page, decisions, issues, live)
    reasons: dict[str, int] = defaultdict(int)  # 只数缺图号、字迹不清这类；“没写单位”之类的备注显示在图纸卡片上
    for p in pages:
        r = p.rec and weak_reason(p.rec)
        if r:
            reasons[r] += 1
    if reasons:
        issues.append(_issue("info", "识别不完整", "weak_rec", f"{sum(reasons.values())} 页识别不完整",
                             "其中" + "、".join(f"{r} {n} 页" for r, n in sorted(reasons.items(), key=lambda x: -x[1]))
                             + "。扫描件或图签不全的页面，识别时选“识别不完整的页面”交给多模态执行器复核。"
                             + ("没读出图号的页先按文件名暂定（图谱里的虚线框）。" if unnumbered else ""), {}, []))

    # ---- 事实层 ----
    facts = _facts(sheets, sheet_out, current_page, notice_out, decisions, issues, live, labels["fact"])

    # ---- 楼层覆盖 ----
    floor_coverage = _floor_coverage(sheet_out, current_page, facts, decisions, issues, live,
                                     sheet_tower if keys.multi else None)

    # ---- 图纸基线 ----
    baselines = []
    by_bare: dict[str, list] = defaultdict(list)
    for k in sheet_out:
        by_bare[bare(k)].append(k)
    for b in baselines_rows:
        snap = {(k if k in sheet_out or len(by_bare.get(k, ())) != 1 else by_bare[k][0]): rev
                for k, rev in json.loads(b["snapshot"]).items()}
        outdated = [{"sheet": no, "base_rev": rev, "current_rev": sheet_out[no]["current_rev"]}
                    for no, rev in sorted(snap.items()) if no in sheet_out and sheet_out[no]["current_rev"] != rev]
        voided = [no for no in snap if no in sheet_out and sheet_out[no]["status"] == "voided"]
        baselines.append({"name": b["name"], "created_at": b["created_at"], "as_of_batch": b["as_of_batch"],
                          "sheets": len(snap), "outdated": outdated, "voided": voided, "note": b["note"]})
        if outdated or voided:
            desc = "；".join(f"{o['sheet']} {o['base_rev']}→{o['current_rev']}" for o in outdated)
            if voided:
                desc += ("；" if desc else "") + "已作废：" + "、".join(voided)
            issues.append(_issue("warn", "基线过期", f"baseline:{b['name']}", f"任务「{b['name']}」用的图纸已更新",
                                 desc + "。", {"sheet": (outdated[0]["sheet"] if outdated else voided[0])}, [],
                                 sheets=[o["sheet"] for o in outdated] + voided))

    # ---- 汇总 ----
    sheets_list = list(sheet_out.values())
    for t, m in sorted(missing.items()):
        out = {"no": t, "number": bare(t), "tower": tower_part(t), "title": m["title"], "discipline": disc(t), "status": "missing",
               "current_rev": None, "current_page": None, "versions": [], "paths": [],
               "inner": False, "notes": m["evidence"], "missing_level": m["level"],
               "confirmed": bool(m.get("confirmed")), "borrowed": False, "own_values": [], "rec_notes": [],
               "cls": classify.sheet_view(lab(t), discipline_of(t)), "fields": {}, "type_kind": _type_kind(t)}
        fl = floors.parse_floors(m["title"] or "", base_for(tower_part(t))[0])
        _set_floors(out, fl, "图名" if fl else "", floors.floor_names(m["title"] or ""))
        sheets_list.append(out)
    sheets_list += prov.values()
    _guess_facts(facts, sheets_list, decisions)
    sheets_list.sort(key=lambda s: (towers.order_key(s.get("tower")) if keys.multi else 0, _disc_order(s["discipline"]),
                                    no_parts(s["no"])[0], no_parts(s["no"])[1] or 0, s["no"]))
    issues.sort(key=lambda i: (ISSUE_ORDER[i["level"]], i["kind"], i["title"]))
    executors = defaultdict(int)
    for p in pages:
        if p.rec and not p.rec_borrowed:
            executors[p.rec["run"]["executor"]] += 1
    return {
        "project": proj.meta, "generated_at": now_iso(), "as_of": as_of,
        "batches": batch_out, "sheets": sheets_list, "edges": edges, "notices": notice_out,
        "issues": issues, "facts": facts, "baselines": baselines, "floor_coverage": floor_coverage,
        "towers": {"multi": keys.multi, "list": [{"name": t, "sheets": sum(1 for x in sheets_list if x.get("tower") == t)}
                                                 for t in keys.all]},
        "guess": {"sheets": sum(1 for e in prov.values() if not e["no"].startswith(NAMELESS)), "nameless": nameless,
                  "pages": sum(len(e["pages"]) for e in prov.values()),
                  "low": sum(1 for e in prov.values() if e["conf"] == names.LOW)},
        "recognition": {"pages": len(pages), "recognized": sum(1 for p in pages if p.rec),
                        "borrowed": sum(1 for p in pages if p.rec_borrowed), "executors": dict(executors)},
    }


def _in_pending(no: str, names: str) -> str | None:
    """图号出现在还没识别的页面的文件名里时，返回那个文件名。"""
    pre, n = no_parts(no)
    if not names or n is None:
        return None
    m = re.search(rf"(?<![A-Za-z0-9\u4e00-\u9fff]){re.escape(pre)}\s*[-－—_]?\s*0*{n}(?![0-9])", names, re.I)
    if not m:
        return None
    start = names.rfind("\n", 0, m.start()) + 1
    end = names.find("\n", m.end())
    return names[start:end if end >= 0 else None]


def _rev_scheme(rev: str | None) -> str | None:
    """版次的写法：字母（A、B）、数字（0、1、第2版）、字母加数字（A1）。写法不同的版次没法比新旧。"""
    r = re.sub(r"[版第次\s]", "", rev or "").upper()
    return "letter" if re.fullmatch(r"[A-Z]+", r) else "number" if re.fullmatch(r"\d+", r) else (
        "mixed" if re.fullmatch(r"[A-Z]+\d+", r) else None)


def _catalog_revisions(sheet_out, current_page, decisions, issues, live, resolve=lambda n, cat: n) -> None:
    """现行目录上写的版次，和收到的现行版次对账。

    目录写的版次更新：可能还缺新版图，手上的旧版不能放心用（严重，逐张提示）。
    目录写的版次更旧：目录没跟着图纸更新（提醒，每份目录合成一条）。
    """
    for cat, p in sorted(current_page.items()):
        if p.rec["page_kind"] != "图纸目录" or cat not in live:
            continue
        cat_rev = sheet_out[cat]["current_rev"]
        behind = []
        for e in p.rec["catalog_entries"]:
            t = resolve(norm_no(e["drawing_no"]), cat) if norm_no(e["drawing_no"]) else None
            if not t or t == cat or t not in live:
                continue  # 没收到的图在“缺图”里提示
            listed, cur = e["revision"], sheet_out[t]["current_rev"]
            if not _rev_scheme(listed) or _rev_scheme(listed) != _rev_scheme(cur) or rev_key(listed) == rev_key(cur):
                continue
            if rev_key(listed) < rev_key(cur):
                behind.append((t, listed, cur))
                continue
            key = f"{t}|{listed}"
            d = _dec(decisions, "catalog_rev", key, f"{bare(t)}|{listed}")
            if d and d["value"] == "ignore":
                continue
            issue = _issue("crit", "目录版次", f"catalog_newer:{cat}:{t}", f"{t} 目录写 {listed} 版，手上只有 {cur} 版",
                           f"目录 {cat}（{cat_rev} 版）列出 {t} 为 {listed} 版，但截至目前收到的最新是 {cur} 版。"
                           f"可能还缺 {listed} 版图纸，手上的 {cur} 版可能已经过时。",
                           {"sheet": t},
                           [{"label": f"列入答疑，向招标方要 {listed} 版", "kind": "catalog_rev", "key": key, "value": "ask"},
                            {"label": f"目录笔误，以 {cur} 版为准", "kind": "catalog_rev", "key": key, "value": "ignore"}],
                           confirmed=bool(d and d["value"] == "ask"))
            issue["qa"] = (f"图纸目录 {cat}（{cat_rev} 版）列出 {t} 为 {listed} 版，但收到的是 {cur} 版，"
                           f"请补发 {listed} 版，或确认以 {cur} 版为准。")
            issues.append(issue)
        if not behind:
            continue
        key = f"{cat}|{cat_rev}|" + ",".join(f"{t}:{cur}" for t, _listed, cur in behind)  # 又有图升版时重新提示
        d = _dec(decisions, "catalog_rev", key, f"{bare(cat)}|{cat_rev}|" + ",".join(f"{bare(t)}:{cur}" for t, _listed, cur in behind))
        if d and d["value"] == "ignore":
            continue
        rows = "；".join(f"{t} 目录 {listed} 版、收到 {cur} 版" for t, listed, cur in behind)
        issue = _issue("warn", "目录版次", f"catalog_behind:{cat}", f"{cat} 目录上的版次没跟上图纸",
                       f"目录 {cat}（{cat_rev} 版）上有 {len(behind)} 张图的版次比收到的旧：{rows}。"
                       "以收到的最新版为准即可；需要以目录为合同依据时，请发图方更新目录。",
                       {"sheet": cat},
                       [{"label": "列入答疑，请发图方更新目录", "kind": "catalog_rev", "key": key, "value": "ask"},
                        {"label": "知道了，以收到的版本为准", "kind": "catalog_rev", "key": key, "value": "ignore"}],
                       confirmed=bool(d and d["value"] == "ask"))
        issue["qa"] = (f"图纸目录 {cat}（{cat_rev} 版）上 " + "、".join(t for t, *_ in behind)
                       + f" 的版次比收到的旧（{rows}），请确认以收到的最新版为准，并提供更新后的图纸目录。")
        issues.append(issue)


def _fact_int(facts: dict, key: str) -> tuple[int | None, str | None]:
    """从事实层取整数（地上层数、地下层数），返回 (值, 说明)。冲突时按当前展示的值，并在说明里写明。"""
    for g in facts["groups"]:
        for f in g["facts"]:
            if f["key"] == key:
                m = re.search(r"\d+", f["value"])
                if not m:
                    return None, None
                note = f"{f['name']} {m.group(0)} 层（{CONF_LABEL[f['conf']]}）"
                if f["conf"] == "conflict":
                    note += "，该事实存在冲突，确认后结果可能变化"
                return int(m.group(0)), note
    return None, None


BASE_LABEL = {1: "Level 00（Ground）是首层，Level 01 是 2F", 0: "Level 01 是首层（1F）"}
BASE_SHORT = {1: "按英式数：Level 00 是首层", 0: "Level 01 是首层"}


def _base_guess(sheets: list) -> tuple[int, str]:
    """英文楼层怎么数（猜）：有 Ground Floor、Level 00（或图号里的楼层代码 00）就按英式，Level 01 是 2F；否则 Level 01 是首层。"""
    for x in sheets:
        m = floors.EN_GROUND.search(x["title"] or "")
        if m:
            return 1, f"图纸里有“{m.group(0)}”（{x['no']}）"
        lv = _level_code(bare(x["no"]))
        if lv in ("00", "GF"):
            return 1, f"图号里有楼层代码 {lv}（{x['no']}）"
    return 0, "图纸里没有 Ground Floor、Level 00"


def _needs_base(sheets: list) -> bool:
    """有没有要看数法才知道第几层的写法：Level 01、1st Floor，或图号里 01、02 这样的楼层代码。"""
    return any(floors.LEVEL_WORD.search(x["title"] or "")
               or re.fullmatch(r"0[1-9]|[1-9]\d", _level_code(bare(x["no"])) or "") for x in sheets)


def _floor_bases(sheets: list, sheet_tower: dict, decisions: dict, multi: bool, issues: list) -> dict:
    """英文楼层的数法：{"*" 或塔楼: (base, 是否人工确认)}（docs/图纸字段.md 9.3）。

    软件先猜、写明理由，问人确认一次；多栋楼各栋猜得不一样时按栋问。没有 Level 01 这类写法的项目不问。"""
    by_t: dict = defaultdict(list)
    for x in sheets:
        by_t[sheet_tower.get(x["no"])].append(x)
    asking = [t for t, xs in by_t.items() if t and _needs_base(xs)] if multi else []
    if len({_base_guess(by_t[t])[0] for t in asking}) <= 1:
        asking = []  # 各栋猜得一样：全项目问一次
    out = {}
    for k, xs in [("*", sheets)] + [(t, by_t[t]) for t in asking]:
        d = decisions.get(("floor_base", k)) or (decisions.get(("floor_base", "*")) if k != "*" else None)
        if d and d["value"] in ("0", "1"):
            out[k] = (int(d["value"]), True)
            continue
        guess, why = _base_guess(xs)
        out[k] = (guess, False)
        if (k == "*" and asking) or not _needs_base(xs):
            continue  # 按栋问了，或这些图里没有要看数法的写法
        who = "这个项目" if k == "*" else k
        issues.append(_issue("warn", "楼层数法", f"floor_base:{k}", f"{'' if k == '*' else k + ' '}英文楼层怎么数？",
                             f"图名里有 Level 01 这样的写法，第几层要看{who}怎么数。{why}，先按“{BASE_LABEL[guess]}”算，请确认。"
                             "同层图纸、楼层覆盖都按这个数。", {},
                             [{"label": BASE_LABEL[v], "kind": "floor_base", "key": k, "value": str(v)} for v in (guess, 1 - guess)]))
    return out


def _level_code(n: str) -> str | None:
    """图号里的楼层代码：ISO 英国附录写法的第四段，或按图号格式（确认前也用）。"""
    lv = (names.iso_parts(n) or {}).get("level")
    if lv:
        return lv
    fmt = _NUMBERING.get()
    got = fmt.level(n) if fmt is not None and n and not n.startswith(NAMELESS) else None
    return got[0] if got else None


def _code_floors(n: str, base: int, sure: bool = True) -> tuple[set[int], str, list, str]:
    """图号里的楼层代码换算成楼层：(楼层, 来源, 原叫法, 把握)。按图号格式的码表（确认前把握低）；
    要看楼层数法的（01 这类）没确认数法时最多算中。"""
    fmt = _NUMBERING.get()
    ip = names.iso_parts(n)
    if ip and ip.get("level"):
        code, meaning, conf, how = ip["level"], None, names.MID, "图号里的楼层代码"
    elif fmt is not None and n and not n.startswith(NAMELESS) and fmt.level(n):
        code, meaning = fmt.level(n)
        conf = names.HIGH if fmt.confirmed else names.LOW
        how = "图号里的楼层代码" + ("（已确认的图号格式）" if fmt.confirmed else f"（{fmt.source}，待确认）")
    else:
        return set(), "", [], names.LOW
    if meaning == numbering.NO_LEVEL:
        return set(), "", [], names.LOW
    fl = (floors.parse_floors(meaning, base) if meaning else set()) or _level_floors(code, base)
    if not fl:
        return set(), "", [], names.LOW
    if not sure and (re.fullmatch(r"0[1-9]|[1-9]\d", code) or floors.LEVEL_WORD.search(meaning or "")):
        conf = lower_conf(conf, names.MID)
    return fl, how, [f"楼层代码 {code}"], conf


def _level_floors(code: str | None, base: int) -> set[int]:
    """ISO 19650 图号里的楼层代码：00 首层，01、02……地上，B1 地下，RF 屋顶；ZZ（多层）、XX（不分层）没有楼层。"""
    c = (code or "").upper()
    if c in ("00", "GF"):
        return {1}
    if re.fullmatch(r"\d{2}", c):
        return {int(c) + base} if int(c) + base > 0 else set()
    m = re.fullmatch(r"B(\d{1,2})", c)
    if m and int(m.group(1)):
        return {-int(m.group(1))}
    if c in ("RF", "RL", "R1", "RF1"):
        return {floors.ROOF}
    return set()


def _sheet_floors(title: str, page, base: int = 1) -> tuple[set[int], str, str]:
    """一张图覆盖的楼层：先看图名；图名没有（如“标准层平面图”）再看“适用楼层”类说明和关键数值。
    返回 (楼层, 从哪看出来的, 那段文字)，那段文字用来留楼层的原叫法。"""
    got = floors.parse_floors(title or "", base)
    if got:
        return got, "图名", title
    rec = page.rec
    for n in rec["content"]["notes"]:
        if floors.STANDARD_HINT.search(n["text"]):
            got = floors.parse_floors(n["text"], base)
            if got:
                return got, "说明", n["text"]
    for kv in rec["content"]["key_values"]:
        if re.search(r"适用(?:楼层|范围)|楼层范围", kv["name"]):
            text = f"{kv['value'] or ''} {kv['raw_text'] or ''}"
            got = floors.parse_floors(text, base)
            if got:
                return got, "说明", text
    texts = []
    for kv in rec["content"]["key_values"]:
        if "层高" in kv["name"]:
            got |= floors.parse_floors(kv["name"].replace("层高", ""), base)
            texts.append(kv["name"].replace("层高", ""))
    return got, ("关键数值" if got else ""), " ".join(texts)


def _first_guess(unrecognized, cad: dict, sheet_out: dict, labels: dict, disc_how, keys: Keys | None = None,
                 ptower: dict | None = None, path_tower: dict | None = None) -> tuple[dict, int]:
    """一级识别：还没看图识别的页面、CAD 文件，只看文件名、文件夹和 PDF 自带的文字，暂定成图纸。

    已看图识别的图号：记在那张图上（pending_files），文件名版次更新的提醒；其余的出一张“按文件名暂定”的图。
    看不出图号的文件也出一张，用“文件：文件名”代替图号——把握低的信息也比没有好。返回 (暂定图纸, 看不出图号的文件数)。"""
    keys, ptower, path_tower = keys or Keys(()), ptower or {}, path_tower or {}

    def pk(p, n):  # 页面的图纸身份
        return keys.key(ptower.get(p.page_id, (None,))[0], n)

    def tower_info(plist, cpaths):
        return ptower.get(plist[0].page_id) if plist else path_tower.get(cpaths[0])
    items: dict[str, list] = defaultdict(list)  # 图纸身份 → [(推测, [页面], CAD 路径)]
    by_file: dict[str, list] = defaultdict(list)
    for p in unrecognized:
        by_file[p.file_sha].append(p)
    for plist in by_file.values():
        if not any("（第 " in x for x in plist[0].paths):  # 单页文件：文件名说的就是这张图
            p = plist[0]
            g = _page_guess(p)
            items[pk(p, norm_no(g["no"]["value"])) if g else _nameless_key(p.paths)].append((g, plist, None))
            continue
        left, found = [], set()  # 多页文件（合订本）：每页先在自己的文字里找图号
        for p in plist:
            g = _page_guess(p)
            if g:
                found.add(norm_no(g["no"]["value"]))
                items[pk(p, norm_no(g["no"]["value"]))].append((g, [p], None))
            else:
                left.append(p)
        if not left:
            continue
        stem = names.split_path(left[0].paths[0])[1]
        rest = [n for n in (norm_no(x) for x in names.listed_numbers(stem)) if n not in found]
        for n in rest:  # 文件名里列了图号：每个图号一张，指向这本里还没认出的几页（把握低）
            items[pk(left[0], n)].append(({"no": {"value": n, "conf": names.LOW, "src": "合订本文件名"}, "title": None, "rev": None,
                              "status": None, "discipline": None, "level": None, "conf": names.LOW, "binder": stem}, left, None))
        if not rest:
            items[_nameless_key(left[0].paths)].append((None, left, None))
    for paths in cad.values():
        g = _fmt_guess(names.guess(paths, ""))
        key = keys.key(path_tower.get(paths[0], (None,))[0], norm_no(g["no"]["value"])) if g else _nameless_key(paths)
        if g or key in items:  # 看不出图号的 CAD 只并到同名的 PDF 上，不单独出图（外部参照之类）
            items[key].append((g, [], paths))
    prov: dict[str, dict] = {}
    nameless = 0
    for key, group in items.items():
        if key in sheet_out:  # 已看图识别的图：同图号还没识别的文件记在它上面
            s = sheet_out[key]
            for g, plist, cpaths in group:
                rev = (g or {}).get("rev") and g["rev"]["value"]
                cur = s["current_rev"]
                newer = bool(rev and cur and _rev_scheme(rev) == _rev_scheme(cur) and (rev_key(rev) or (0, 0)) > (rev_key(cur) or (0, 0)))
                for x in plist:
                    s.setdefault("pending_files", []).append({"page_id": x.page_id, "path": x.paths[0], "rev": rev, "newer": newer})
                if cpaths:
                    s.setdefault("cad", []).extend(cpaths)
                if newer:
                    s["notes"].append(f"文件名显示有更新的 {rev} 版（{(plist[0].paths[0] if plist else cpaths[0]).split(' / ')[-1]}），"
                                      "还没看图识别；识别后再确认现行版。")
            continue
        prov[key] = _guess_sheet(key, group, labels, disc_how, keys, tower_info)
        nameless += key.startswith(NAMELESS)
    return prov, nameless


def _page_guess(p) -> dict | None:
    """一页的一级识别；看图识别读出了图名（只是没读出图号）就用读出的图名。"""
    g = _fmt_guess(names.guess(p.paths, p.text))
    title = _val(p.rec, "drawing_title") if p.rec else None
    if g and title:
        g["title"] = {"value": title, "conf": names.MID, "src": "看图识别读出的图名"}
    return g


def _nameless_key(paths: list) -> str:
    return NAMELESS + names.split_path(paths[0])[1]


def _guess_sheet(key: str, group: list, labels: dict, disc_how, keys: Keys | None = None, tower_info=None) -> dict:
    """一张“按文件名暂定”的图：各项推测带来源和把握（高、中、低），图谱里画虚线框。"""
    def rk(item):
        g = item[0] or {}
        rev = g["rev"]["value"] if g.get("rev") else None
        return (bool(item[1]), rev_key(rev) or (0, 0), bool(rev), -min((x.first_batch for x in item[1]), default=0))  # 00 版也比没写版次的强
    best = max(group, key=rk)
    g, plist, cpaths = best
    g = g or {}
    paths = plist[0].paths if plist else cpaths
    stem = names.split_path(paths[0])[1]
    pages = list(dict.fromkeys(x.page_id for _g, pl, _c in group for x in pl))
    title = (g.get("title") or {}).get("value")
    seen = [x for _g, pl, _c in group for x in pl if x.rec is not None]  # 看图识别过、只是没读出图号的页面
    if key.startswith(NAMELESS):
        title = next((_val(x.rec, "drawing_title") for x in seen if _val(x.rec, "drawing_title")), None) \
            or names.parse_name(stem)["title"] or stem
    where = {"page_id": plist[0].page_id if plist else None, "path": paths[0]}

    def env(x):  # 一级识别推出的一项，补上出处
        return fld(x["value"], x["src"], x["conf"], where=where) if x else None
    fields = {k: env(g.get(k)) for k in ("no", "title", "rev")}
    tw, fields["tower"] = _tower_field(key, (tower_info and tower_info(plist, cpaths)) or (None, "", ""), keys or Keys(()))
    if fields["tower"]:
        fields["tower"]["where"] = where
    fields["issue_status"] = fld(g.get("status"), "文件名", names.MID, where=where)
    if key.startswith(NAMELESS):
        fields["no"] = {"value": None, "raw": None, "src": "文件名里看不出图号", "conf": names.LOW, "where": where}
        fields["title"] = fld(title, "文件名", names.LOW, where=where)
    d, dsrc, dconf = disc_how(key, paths)
    if d != "其他":
        gd = g.get("discipline")
        fields["discipline"] = env(gd) if gd and gd["value"] == d else fld(d, dsrc, dconf, where=None if dsrc == AI_SRC else where)
    else:
        w = names.words_discipline(title or "")
        d = w or "其他"
        fields["discipline"] = fld(d, "图名里的词" if w else "认不出", names.LOW, where=where)
    if g.get("binder"):
        fields["content"] = fld(f"合订本“{g['binder']}”里的一张，具体在哪页要看图识别", "合订本文件名", names.LOW, where=where)
    elif key.startswith(NAMELESS) and len(pages) > 1:
        fields["content"] = fld(f"{len(pages)} 页的合订本，看不出里面有哪些图", "文件名", names.LOW, where=where)
    conf = g.get("conf") or names.LOW
    versions = []
    for gg, pl, cp in group:
        for x in pl or [None]:
            versions.append({"rev": (gg or {}).get("rev") and gg["rev"]["value"], "date": None,
                             "batch": x.first_batch if x else None, "state": "按文件名暂定",
                             "page_id": x.page_id if x else None, "paths": x.paths if x else cp})
    return {
        "no": key, "number": None if key.startswith(NAMELESS) else bare(key), "tower": tw,
        "title": title, "discipline": d, "status": "guess",
        "current_rev": (g.get("rev") or {}).get("value"), "current_page": plist[0].page_id if plist else None,
        "versions": versions, "paths": paths, "inner": all(_in_nested_archive(x) for x in paths),
        "notes": [], "borrowed": False, "own_values": [], "rec_notes": [],
        "cls": classify.sheet_view(labels["sheet"].get(key) or labels["sheet"].get(bare(key)), discipline_of(key)),
        "fields": fields, "conf": conf, "pages": pages, "seen": bool(seen),
        "cad": [x for _g, _pl, cp in group if cp for x in cp], "type_kind": _type_kind(key),
    }


def _name_checks(sheet_out, current_page, decisions, issues, live) -> None:
    """看图识别出的图号、版次和文件名对不上：可能是文件名写错，也可能是识别看错了，列出来让人核对。"""
    for no, cp in sorted(current_page.items()):
        if no not in live or cp.rec["page_kind"] == "图纸目录":
            continue
        g = _fmt_guess(names.guess(cp.paths, ""))
        if not g or not g["no"]["src"].startswith("文件名"):
            continue
        gno, diffs, num = norm_no(g["no"]["value"]), [], bare(no)
        if gno != num and not (gno.endswith("-" + num) or num.endswith("-" + gno)):
            diffs.append(f"图号：文件名写 {gno}，图签是 {num}")
        grev, cur = (g.get("rev") or {}).get("value"), sheet_out[no]["current_rev"]
        if grev and cur and _rev_scheme(grev) and _rev_scheme(grev) == _rev_scheme(cur) and rev_key(grev) != rev_key(cur):
            diffs.append(f"版次：文件名写 {grev}，图签是 {cur}")
        if not diffs:
            continue
        fname = cp.paths[0].split(" / ")[-1]
        key = f"{no}|{fname}"
        d = _dec(decisions, "name_check", key, f"{num}|{fname}")
        if d and d["value"] == "ignore":
            continue
        issue = _issue("warn", "文件名不符", f"name:{no}", f"{no} 文件名和图签对不上",
                       f"{fname}：" + "；".join(diffs) + "。台账以图签为准；也可能是识别看错了，打开原图核对。",
                       {"sheet": no},
                       [{"label": "列入答疑", "kind": "name_check", "key": key, "value": "ask"},
                        {"label": "知道了，以图签为准", "kind": "name_check", "key": key, "value": "ignore"}],
                       confirmed=bool(d and d["value"] == "ask"))
        issue["qa"] = f"文件 {fname} 的文件名与图签不一致（{'；'.join(diffs)}），请确认以哪个为准。"
        issues.append(issue)


def _guess_facts(facts: dict, sheets: list, decisions: dict | None = None) -> None:
    """还没从图上读到层数时，按图名里出现的楼层推测地上、地下层数（低置信，不参与冲突和楼层覆盖检查）。"""
    have = {f["key"] for g in facts["groups"] for f in g["facts"]}
    fl = [(f, s["no"]) for s in sheets if s["status"] not in ("missing", "voided") for f in s.get("floors") or []]
    above = [x for x in fl if 0 < x[0] < floors.ROOF]
    below = [x for x in fl if x[0] < 0]
    add = []
    if "地上层数" not in have and above:
        top = max(f for f, _ in above)
        if top >= 2:
            srcs = sorted({no for f, no in above if f == top})
            add.append(("地上层数", f"{top}层", f"按图名推测：最高到 {floors.label(top)}（{'、'.join(srcs[:3])}）。图上的说明识别出来后以说明为准。", srcs))
    if "地下层数" not in have and below:
        low = min(f for f, _ in below)
        srcs = sorted({no for f, no in below if f == low})
        add.append(("地下层数", f"{-low}层", f"按图名推测：最低到 {floors.label(low)}（{'、'.join(srcs[:3])}）。图上的说明识别出来后以说明为准。", srcs))
    if not add:
        return
    group = next((g for g in facts["groups"] if g["id"] == "工程概况"), None)
    if group is None:
        group = {"id": "工程概况", "label": "工程概况", "facts": []}
        facts["groups"].insert(0, group)
    for key, value, note, srcs in add:
        conf, d = "low", (decisions or {}).get(("fact", key))
        if d:  # 人工确认过推测的值
            conf, value = "confirmed", d["value"]
            note = f"人工确认（{d['created_at'][:10]}）" + (f"：{d['note']}" if d["note"] else "") + "。" + note
        group["facts"].append({"key": key, "name": key, "value": value, "conf": conf, "note": note, "evidence": [],
                               "sources": srcs[:5], "notices": [], "path": [], "std": False, "ai": False, "guess": True})
    group["facts"].sort(key=lambda f: f["name"])


def _set_floors(s: dict, fl: set[int], how: str, raw=()) -> None:
    """楼层：换算出的第几层（floors、floors_text），加上原叫法显示成“Level 05（6F）”（floors_name）。"""
    title = s["title"] or ""
    s["floors"], s["floors_from"], s["floors_text"] = sorted(fl), how, floors.compress(fl) or None
    name = "、".join(list(raw)[:3])
    s["floors_name"] = (f"{name}（{s['floors_text']}）" if name and name != s["floors_text"] else s["floors_text"]) if fl else None
    # 通用图只看图名：没写楼层的柱平面布置图，即使层高表里列了各层，也是建哪一层都要看的图
    s["general"] = bool(GENERAL_SHEET.search(title)) and "目录" not in title and not floors.parse_floors(title)


def _same_building(a: dict, b: dict) -> bool:
    """同一栋楼的图才算同层；公共、裙楼、地下室和看不出塔楼的，跟每一栋都算。"""
    x, y = a.get("tower"), b.get("tower")
    return not x or not y or x == y or x in towers.SHARED or y in towers.SHARED


def same_floor(st: dict, no: str, limit: int | None = None) -> dict:
    """一张图的同层图纸（各专业管同一层的图）和通用图（设计说明、没写楼层的柱平面等）。

    同层的按专业排，楼层完全一样的排前面；有严重问题的图带上问题标题，从这里就能看到。"""
    by_no = {s["no"]: s for s in st["sheets"]}
    s = by_no[no]
    crit = defaultdict(list)
    for i in st["issues"]:
        if i["level"] == "crit" and i["kind"] != "缺图" and i["target"].get("sheet"):  # 缺图看状态就知道
            crit[i["target"]["sheet"]].append(i["title"])

    def row(t):
        return {"no": t["no"], "title": t["title"], "discipline": t["discipline"], "status": STATUS_LABEL[t["status"]],
                "floors": t["floors_text"], "problems": crit.get(t["no"], [])}

    mine = set(s["floors"])
    near = [t for t in st["sheets"] if t["no"] != no and t["status"] != "voided" and not t["general"]
            and mine & set(t["floors"]) and _same_building(s, t)]
    near.sort(key=lambda t: (_disc_order(t["discipline"]), len(mine ^ set(t["floors"])), no_parts(t["no"])[0],
                             no_parts(t["no"])[1] or 0))
    general = [t for t in st["sheets"] if t["no"] != no and t["status"] != "voided" and t["general"] and _same_building(s, t)]
    general.sort(key=lambda t: (_disc_order(t["discipline"]), no_parts(t["no"])[0], no_parts(t["no"])[1] or 0))
    out = {"floors": s["floors_text"], "same_floor": [row(t) for t in near[:limit]], "general": [row(t) for t in general]}
    if limit is not None and len(near) > limit:
        out["more_same_floor"] = len(near) - limit
    return out


def _floor_coverage(sheet_out, current_page, facts, decisions, issues, live, sheet_tower=None) -> list[dict]:
    above, above_note = _fact_int(facts, "地上层数")
    below, below_note = _fact_int(facts, "地下层数")
    out = []
    per_tower = sheet_tower is not None  # 多栋楼：每栋各查各的；层数事实是全项目的，不当某一栋的层数用
    tws = sorted({sheet_tower.get(no) for no in live}, key=towers.order_key) if per_tower else [None]
    for tw, (disc, gname, include, exclude, full) in [(tw, g) for tw in tws for g in floors.GROUPS]:
        if per_tower:
            gname = f"{tw or towers.UNKNOWN} {gname}"
            full = full and tw is not None and tw not in towers.SHARED  # 裙楼、地下室、公共只查断档
        members = []
        for no in sorted(live):
            s = sheet_out[no]
            title = s["title"] or ""
            if per_tower and sheet_tower.get(no) != tw:
                continue
            if s["discipline"] != disc or not re.search(include, title) or (exclude and re.search(exclude, title)):
                continue
            members.append((no, title, set(s["floors"]), s["floors_from"]))
        if not members:
            continue
        covered: dict[int, list[str]] = defaultdict(list)
        for no, _title, fl, _how in members:
            for f in fl:
                covered[f].append(no)
        pending = {no for no, *_ in members if sheet_out[no]["status"] == "pending_void"}  # 作废还是漏发待确认
        firm = {f for no, _t, fl, _h in members if no not in pending for f in fl}
        tentative = sorted(set(covered) - firm)
        unknown = [m for m in members if not m[2]]
        for no, title, _fl, _how in unknown:
            if "标准层" in title:
                issues.append(_issue("warn", "楼层覆盖", f"floor_unknown:{no}", f"{no} {title} 未注明楼层范围",
                                     "图名和说明里都没有写适用楼层，无法判断标准层覆盖了哪几层。", {"sheet": no}, []))
        ground = sorted(f for f in covered if 0 < f < floors.ROOF)
        basis = None
        if full:
            top = (None if per_tower else above) or (max(ground) if ground else None)
            expected = set(range(1, top + 1)) if top else set()
            expected |= {-n for n in range(1, ((None if per_tower else below) or 0) + 1)}
            expected.add(floors.ROOF)
            basis = (f"按 {tw} 的图纸里出现的最高楼层 {top}F 检查" if per_tower and top else
                     "；".join(x for x in (above_note, below_note) if x) or (
                         f"事实层没有层数，按图纸里出现的最高楼层 {top}F 检查" if top else None))
        elif len(ground) >= 2:
            expected = set(range(min(ground), max(ground) + 1))
            basis = f"只检查已出现楼层 {floors.label(min(ground))}~{floors.label(max(ground))} 之间的断档"
        else:
            expected = set()
            basis = ("图名和说明里都没有楼层信息，无法检查" if not ground
                     else f"只知道 {floors.label(ground[0])} 一层，无法判断有没有断档")
        missing = sorted(expected - set(covered))
        unchecked = None
        if not full and expected and above and not per_tower:  # 只查了已出现楼层之间：说清楚楼里还有哪些层没查
            building = set(range(1, above + 1)) | {-n for n in range(1, (below or 0) + 1)} | {floors.ROOF}
            unchecked = floors.compress(sorted(building - expected - set(covered))) or None
        remarks = []
        if unchecked:
            remarks.append(f"没查：{unchecked}（这类图只检查已出现楼层之间有没有断档）")
        if tentative:
            src = sorted({no for f in tentative for no in covered[f] if no in pending})
            remarks.append(f"{floors.compress(tentative)} 只有待确认是否作废的 {'、'.join(src)}，确认作废后会变成缺图")
        row = {
            "group": gname, "discipline": disc, "basis": basis,
            "covered": [{"floors": floors.compress(fl), "sheets": [no], "from": how, "tentative": no in pending}
                        for no, _t, fl, how in members if fl],
            "covered_text": floors.compress(covered),
            "unknown": [no for no, *_ in unknown],
            "missing": [floors.label(f) for f in missing], "missing_text": floors.compress(missing),
            "status": "missing" if missing else ("ok" if full and expected else "no_gap" if expected else "unchecked"),
            "unchecked": unchecked, "tentative": floors.compress(tentative) or None, "remarks": remarks,
        }
        if missing:
            key = f"楼层|{gname}|{row['missing_text']}"
            d = decisions.get(("missing", key))
            if d and d["value"] == "not_needed":
                row["status"] = "accepted"
            else:
                row["confirmed"] = bool(d and d["value"] == "ask")
                if len(row["covered"]) > COVER_LIST_MAX:  # 几十上百张图：只写楼层范围
                    have = f"{row['covered_text']}（{len(row['covered'])} 张图）" + (
                        f"，其中 {row['tentative']} 只有待确认是否作废的图" if row["tentative"] else "")
                else:
                    have = "；".join(f"{c['floors']}（{c['sheets'][0]}{'，待确认是否作废' if c['tentative'] else ''}）"
                                    for c in row["covered"]) or "无"
                issues.append(_issue(
                    "warn", "楼层覆盖", f"floor:{gname}", f"{gname}缺 {row['missing_text']}",
                    f"{basis}。已有：{have}。缺：{row['missing_text']}。", {"floor_group": gname},
                    [{"label": "列入答疑，向招标方要图", "kind": "missing", "key": key, "value": "ask"},
                     {"label": "确认不需要（已有图纸覆盖或该层不存在）", "kind": "missing", "key": key, "value": "not_needed"}],
                    level_word="中", confirmed=row["confirmed"]))
        out.append(row)
    return out


ISSUE_ORDER = {"crit": 0, "warn": 1, "info": 2}
RISK_DECISION = {"safe": "已确认安全", "dangerous": "已确认有风险"}


def _security(rows, scans, paths, decisions, issues) -> dict:
    """风险文件：按内容（SHA-256）合并各项发现。高风险算严重问题，中风险算提醒，低风险只写进报告。"""
    by_sha: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: risk.LEVEL_ORDER[r["level"]]):
        f = by_sha.setdefault(r["sha256"], {"sha": r["sha256"], "key": r["sha256"][:12], "level": r["level"],
                                            "findings": [], "sources": [], "first_batch": r["batch_seq"],
                                            "blocked": r["kind"] == "av_blocked"})
        if risk.LEVEL_ORDER[r["level"]] < risk.LEVEL_ORDER[f["level"]]:
            f["level"] = r["level"]
        if r["detail"] not in f["findings"]:
            f["findings"].append(r["detail"])
        if r["source"] not in f["sources"]:
            f["sources"].append(r["source"])
        if r["sha256"] not in paths and r["path"] not in f.setdefault("_paths", []):
            f["_paths"].append(r["path"])
    files = []
    for sha, f in by_sha.items():
        f["paths"] = paths.get(sha) or f.pop("_paths", [])
        f.pop("_paths", None)
        f["name"] = f["paths"][0].split(" / ")[-1] if f["paths"] else "（未定位）"
        f["level_word"] = risk.LEVEL_WORD[f["level"]]
        d = decisions.get(("risk", f["key"]))
        f["decision"] = d["value"] if d else None
        files.append(f)
        if f["level"] == "low" or f["decision"] == "safe":
            continue
        where = "；".join(f["paths"][:3]) + (f" 等 {len(f['paths'])} 处" if len(f["paths"]) > 3 else "")
        if f["blocked"]:  # 文件已被杀毒软件拦下，项目里没有它
            title, tail = f"{f['name']}（被杀毒软件拦截）", "如果它是图纸，需要发图方重新发一份干净的文件。"
            labels = ("确认是误报（来源可信）", "列入答疑，请发图方核实并重发")
        else:
            title, tail = f"{f['name']}（{f['level_word']}风险）", "不要双击打开，也不要和图纸放在同一个文件夹里用 CAD 打开。"
            labels = ("确认安全（来源可信）", "确认有风险，列入答疑请发图方核实")
        issues.append(_issue(
            "crit" if f["level"] == "high" else "warn", "风险文件", f"risk:{f['key']}", title,
            "；".join(f["findings"]) + f"。位置：{where}。{tail}",
            {"file": f["key"]},
            [{"label": labels[0], "kind": "risk", "key": f["key"], "value": "safe"},
             {"label": labels[1], "kind": "risk", "key": f["key"], "value": "dangerous"}],
            level_word=f["level_word"], confirmed=f["decision"] == "dangerous"))
    files.sort(key=lambda f: (risk.LEVEL_ORDER[f["level"]], f["name"]))
    pending = sum(1 for f in files if f["level"] != "low" and not f["decision"])
    return {"files": files, "scans": scans, "pending": pending}


def _disc_order(d: str) -> int:
    order = classify.DISCIPLINES
    return order.index(d) if d in order else len(order)


def _issue(level, kind, iid, title, detail, target, options, level_word=None, confirmed=False, sheets=None):
    """sheets：牵涉到的其他图纸（事实冲突的各个来源、引用了缺图的图纸等），图纸卡片上作为“相关问题”显示。"""
    return {"id": iid, "level": level, "kind": kind, "title": title, "detail": detail, "target": target,
            "options": options, "level_word": level_word, "confirmed": confirmed, "sheets": sorted(set(sheets or ()))}


BASIS_NOTE = re.compile(r"[（(][^（）()]*(?:含|包括|计入|不计|除)[^（）()]*[）)]")  # 统计口径的说明，如“（含屋顶机房层）”


def _basis_quote(evs: list) -> str:
    """来源原文带统计口径说明时引出来：数不一样，可能只是口径不同。"""
    t = next((e["text"] for e in evs if e["text"] and BASIS_NOTE.search(e["text"])), None)
    return f"，原文“{t}”" if t else ""


def _facts(sheets, sheet_out, current_page, notices, decisions, issues, live, fact_labels=None):
    """项目事实，按大类分组；每条带 path（大类下面的小类）：智能分类过的按 AI 的，没分过的按说法粗分。"""
    fact_labels = fact_labels or {}
    ev_by_key: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    is_std: dict[str, bool] = {}

    def add(key, name, value, unit, raw, sheet, rev, page, source, conf, active, std=False):
        names.setdefault(key, name)
        is_std[key] = std
        ev_by_key[key].append({"sheet": sheet, "rev": rev, "page_id": page.page_id, "value": value, "unit": unit,
                               "text": raw, "source": source, "confidence": conf, "active": active})

    for no, s in sheets.items():
        if no not in live:
            continue
        cur = current_page[no]
        for rev, p in s.pages:
            active = p.render_hash == cur.render_hash or (rev == sheet_out[no]["current_rev"] and p is cur)
            for kv in p.rec["content"]["key_values"]:
                if kv["value"] is None:
                    continue
                if SHEET_SCOPED.search(norm_fact_key(kv["name"])):  # 本层标高、适用楼层：只记在这张图上，不跨图比
                    own = sheet_out[no]["own_values"]
                    item = {"name": kv["name"], "value": str(kv["value"]), "unit": kv["unit"], "text": kv["raw_text"]}
                    if active and item not in own:
                        own.append(item)
                    continue
                add(norm_fact_key(kv["name"]), kv["name"], str(kv["value"]), kv["unit"], kv["raw_text"],
                    no, rev, p, kv["source"], kv["confidence"], active)
            for st in p.rec["content"]["standards"]:
                if not st["code"]:
                    continue
                key = re.sub(r"[（(].*$", "", st["code"]).replace(" ", "")
                add(key, st["name"] or key, st["code"], None, st["code"] + (f" {st['name']}" if st["name"] else ""),
                    no, rev, p, st["source"], st["confidence"], active, std=True)

    groups: dict[str, list] = defaultdict(list)
    for key, evs in ev_by_key.items():
        # 同一页面重复出现的同值证据只算一次
        uniq, seen = [], set()
        for e in evs:
            k = (e["sheet"], e["rev"], e["page_id"], norm_value(e["value"]))
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        act = [e for e in uniq if e["active"]]
        stale = [e for e in uniq if not e["active"]]
        if not act:
            continue
        values = defaultdict(list)
        for e in act:
            values[norm_value(e["value"])].append(e)
        ranked = sorted(values.values(), key=lambda es: (-len({e["sheet"] for e in es}), es[0]["sheet"]))
        best = ranked[0]
        display = best[0]["value"] + (f" {best[0]['unit']}" if best[0]["unit"] and best[0]["unit"] not in ("层", "度") else (best[0]["unit"] or ""))
        note = None
        d = decisions.get(("fact", key))
        if d:
            conf = "confirmed"
            display = d["value"]
            note = f"人工确认（{d['created_at'][:10]}）" + (f"：{d['note']}" if d["note"] else "")
        elif len(values) > 1:
            conf = "conflict"
            parts = "；".join(f"{es[0]['value']}（{'、'.join(sorted({e['sheet'] for e in es}))}{_basis_quote(es)}）" for es in ranked)
            note = f"来源不一致：{parts}。"
            if any(_basis_quote(es) for es in ranked):
                note += "带统计口径说明的，可能只是口径不同（例如含不含机房层），不一定是图纸矛盾，核对原文后再定。"
            issues.append(_issue("crit", "事实冲突", f"fact:{key}", f"{names[key]}：" + " / ".join(es[0]["value"] for es in ranked),
                                 note, {"fact": key},
                                 [{"label": f"采用 {es[0]['value']}", "kind": "fact", "key": key,
                                   "value": es[0]["value"] + (f" {es[0]['unit']}" if es[0]["unit"] else "")} for es in ranked],
                                 sheets={e["sheet"] for e in act}))
        else:
            srcs = {e["sheet"] for e in best}
            if len(srcs) >= 2:
                conf = "high"
            elif all(e["source"] in ("text_layer", "both") for e in best):
                conf = "mid"
            else:
                conf = "low"
        changed = [e for e in stale if norm_value(e["value"]) not in values]
        if changed and conf != "confirmed":
            old = changed[0]
            up = f"来源已升版：{old['sheet']} {old['rev']} 版为 {old['value']}，现行版为 {best[0]['value']}。"
            note = (note + " " if note else "") + up
        # 出处图全都被变更单改过：数值可能已经不对了（变更单的内容不进事实层，要打开变更单核对）
        act_sheets = {e["sheet"] for e in act}
        by_notice = sorted({n["id"] for n in notices if act_sheets & set(n["affects"])})
        if by_notice and conf != "confirmed" and all(any(x in n["affects"] for n in notices) for x in act_sheets):
            note = (note + " " if note else "") + (
                f"出处 {'、'.join(sorted(act_sheets))} 有{'、'.join(by_notice)}，这个数值可能已被修改，以变更单为准。")
        else:
            by_notice = []
        place = classify.fact_place(fact_labels.get(classify.fact_template(names[key])), is_std[key])
        top, path = place or (fact_group(names[key], is_std[key]), None)
        groups[top].append({
            "key": key, "name": names[key], "value": display, "conf": conf, "note": note,
            "evidence": act + stale, "sources": sorted(act_sheets), "notices": by_notice,
            "path": path, "std": is_std[key], "ai": place is not None,
        })
    for fs in groups.values():  # 还没智能分类的：同一说法有两条以上的归成一个小类（KL3 截面、Q1 截面 → 截面）
        fam = {f["key"]: classify.fact_family(f["name"]) for f in fs if f["path"] is None}
        n = Counter(fam.values())
        for f in fs:
            if f["path"] is None:
                f["path"] = [fam[f["key"]]] if n[fam[f["key"]]] >= 2 else []
    return {"groups": [{"id": g, "label": g, "facts": sorted(groups[g], key=lambda f: f["name"])}
                       for g in classify.FACT_TOPS if groups.get(g)]}


# ---------------- 差异报告 ----------------

def diff_states(prev: dict | None, cur: dict) -> dict:
    ps = {s["no"]: s for s in (prev or {}).get("sheets", [])}
    cs = {s["no"]: s for s in cur["sheets"]}
    out = {"batch": cur["as_of"], "upgraded": [], "added": [], "resolved_missing": [], "new_missing": [],
           "new_conflicts": [], "new_fact_conflicts": [], "pending_void": [], "fact_changes": [], "baselines": [],
           "floor_gaps": []}
    for no, s in cs.items():
        p = ps.get(no)
        if p is None and SEP in no:  # 加塔楼以前的身份是光图号：同一栋楼的才对得上
            q = ps.get(bare(no))
            p = q if q and q.get("tower") in (None, s.get("tower")) else None
        if s["status"] == "missing":
            if not p or p["status"] != "missing":
                out["new_missing"].append(no)
            continue
        if p is None or p["status"] == "missing":
            out["added"].append(no)
            if p and p["status"] == "missing":
                out["resolved_missing"].append(no)
        elif p["current_rev"] != s["current_rev"]:
            out["upgraded"].append(f"{no} {p['current_rev']}→{s['current_rev']}")
        if s["status"] == "conflict" and (not p or p["status"] != "conflict"):
            out["new_conflicts"].append(no)
        if s["status"] == "pending_void" and (not p or p["status"] != "pending_void"):
            out["pending_void"].append(no)
    pf = {f["key"]: f for g in (prev or {}).get("facts", {}).get("groups", []) for f in g["facts"]}
    for g in cur["facts"]["groups"]:
        for f in g["facts"]:
            old = pf.get(f["key"])
            moved = _source_changes(old, f) if old else ""
            if old and (old["value"] != f["value"] or old["conf"] != f["conf"] or moved):
                out["fact_changes"].append(f"{f['name']}：{old['value']}（{CONF_LABEL[old['conf']]}）→ {f['value']}（{CONF_LABEL[f['conf']]}）"
                                           + (f"。{moved}" if moved else ""))
            elif not old and prev is not None:
                out["fact_changes"].append(f"新增 {f['name']}：{f['value']}")
            if prev is not None and f["conf"] == "conflict" and (not old or old["conf"] != "conflict"):
                out["new_fact_conflicts"].append(f"{f['name']}：{_values_text(f)}" + (f"。{moved}" if moved else ""))
    prev_gaps = {r["group"]: r["missing_text"] for r in (prev or {}).get("floor_coverage", []) if r["status"] == "missing"}
    for r in cur.get("floor_coverage", []):
        if r["status"] == "missing" and prev_gaps.get(r["group"]) != r["missing_text"]:
            out["floor_gaps"].append(f"{r['group']}缺 {r['missing_text']}")
    for b in cur["baselines"]:
        if b["outdated"]:
            out["baselines"].append(f"{b['name']}：" + "、".join(f"{o['sheet']} {o['base_rev']}→{o['current_rev']}" for o in b["outdated"]))
    return out


def _values_text(f: dict) -> str:
    """现行来源各自写的值：“20（建施-01、建施-07） / 21（结施-01）”。"""
    by_value: dict[str, list] = defaultdict(list)
    for e in f["evidence"]:
        if e["active"]:
            by_value[norm_value(e["value"])].append(e)
    ranked = sorted(by_value.values(), key=lambda es: -len({e["sheet"] for e in es}))
    return " / ".join(f"{es[0]['value']}（{'、'.join(sorted({e['sheet'] for e in es}))}）" for es in ranked)


def _source_changes(old: dict, new: dict) -> str:
    """事实的来源里哪张图改了数：“结施-01 B 版改为 21（原 A 版为 20）”。"""
    before = {e["sheet"]: e for e in old["evidence"] if e["active"]}
    seen_values = {norm_value(e["value"]) for e in before.values()}
    out = []
    for e in new["evidence"]:
        if not e["active"]:
            continue
        b = before.get(e["sheet"])
        rev = f" {e['rev']} 版" if e["rev"] else ""
        if b and norm_value(b["value"]) != norm_value(e["value"]):
            out.append(f"{e['sheet']}{rev}改为 {e['value']}（原{' ' + b['rev'] + ' 版' if b['rev'] else ''}为 {b['value']}）")
        elif not b and norm_value(e["value"]) not in seen_values:
            out.append(f"{e['sheet']}{rev}写的是 {e['value']}")
    return "；".join(dict.fromkeys(out))


def create_baseline(proj: Project, name: str, sheets: list[str] | None = None, note: str | None = None) -> dict:
    """锁定一份图纸基线：记下当前每张图的现行版次。以后图纸升版时，系统会提示这个任务用的图过期了。"""
    st = build(proj, with_diff=False)
    pick = {norm_no(x) for x in sheets} if sheets else None
    snap = {s["no"]: s["current_rev"] for s in st["sheets"]
            if s["current_rev"] and s["status"] not in ("missing", "voided") and (pick is None or s["no"] in pick)}
    if pick:
        unknown = pick - set(snap)
        if unknown:
            raise ValueError("这些图号不在现行图纸中：" + "、".join(sorted(unknown)))
    with proj.connect() as con:
        con.execute("INSERT OR REPLACE INTO baselines(name, created_at, as_of_batch, snapshot, note) VALUES (?,?,?,?,?)",
                    (name, now_iso(), st["as_of"], json.dumps(snap, ensure_ascii=False), note))
    return snap
