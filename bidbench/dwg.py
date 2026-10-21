"""读 DWG、DXF：图签、布局、图上的文字（docs/图纸字段.md 9.7）。

DWG 用 LibreDWG 的 dwgread 读成 JSON（单独的程序，随安装包附带，软件只调用它，不链接）。JSON 很大（几 MB 的 DWG
出来上百 MB），边读边筛，只留文字、块属性、布局、视口和块定义里的线；文字里夹着的坏字节照样容错读。DXF 软件自己读。

读出来的东西整理成每张图一份：
    - 带图号属性的图签块每有一个就是一张图（一个模型空间里排几张图很常见）；图签外框里的文字归这张图；
    - 布局里没有这种图签块的，每个有内容的布局算一张图：图号从图签文字（“Sheet No.”旁边的字）或布局名认；
    - 模型空间里的文字按各布局视口看到的范围归到那张图；没有布局的，整个模型空间算一张图。
每张图写成一份和看图识别同样格式的结果（spec/schema.json，执行器 dwg，来源 cad），state.py 照常套用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import debuglog, names, proc

log = logging.getLogger(__name__)

EXECUTOR = "dwg"
INSTRUCTION_VERSION = "dwg-4"  # 认图签、分图的规则改了就加 1；按旧规则读的文件下次读 DWG（收图后、点“读 DWG”）时自动重读
ATTR_CONF = 0.95  # 图签块属性：图上原样写的，把握高
NEAR_CONF = 0.75  # 按位置配对的图签文字、按值认的图号、布局名：中
TEXT_CONF = 0.7   # 从文字里找出的引用、目录行：中
MODEL = "模型"


class ReadError(Exception):
    """读不出：给人看的原因。"""


def timeout_for(size: int) -> int:
    """每个文件最多读多久：至少 60 秒，大文件每 MB 加 3 秒，最多 15 分钟。"""
    return int(min(900, max(60, size / (1 << 20) * 3)))


# ---------------- 读出来的东西 ----------------

@dataclass
class Text:
    text: str
    x: float
    y: float
    space: str
    h: float = 0.0
    tag: str | None = None      # 块属性的属性名；单行、多行文字为 None
    callout: bool = False       # 来自不是图签的块属性（详图索引符号之类）
    mark: str | None = None     # 索引符号里的详图编号（同一个块的另一个属性，⑤/建施-12 的 5）


@dataclass
class Insert:
    block: str
    x: float
    y: float
    sx: float
    sy: float
    rot: float
    space: str
    attrs: list = field(default_factory=list)  # [(属性名, 值, x, y)]
    rng: tuple | None = None                   # R2000 的第一个、最后一个属性句柄
    nested: bool = False                       # 块里套的块（动态块的轴号圈之类）：属性当图上的字，不当图签


@dataclass
class Viewport:
    space: str
    cx: float       # 在布局上的中心、宽、高
    cy: float
    w: float
    h: float
    vx: float       # 看到的模型空间：中心（视图坐标，扭转、目标点之前）、高
    vy: float
    vh: float
    twist: float = 0.0  # 视口里的图转了多少度（DXF 组码 51）
    tx: float = 0.0     # 目标点（DXF 组码 17、27）：视图坐标的原点在模型空间里的位置
    ty: float = 0.0

    def _to_model(self, x: float, y: float) -> tuple[float, float]:
        """视图坐标 → 模型空间：转回扭转角，再加目标点。"""
        r = math.radians(-self.twist)
        return self.tx + x * math.cos(r) - y * math.sin(r), self.ty + x * math.sin(r) + y * math.cos(r)

    def window(self) -> tuple:
        """看到的模型空间范围（转了角度的取外接矩形）。"""
        vw = self.vh * self.w / self.h
        pts = [self._to_model(self.vx + dx * vw / 2, self.vy + dy * self.vh / 2) for dx in (-1, 1) for dy in (-1, 1)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def to_paper(self, x: float, y: float) -> tuple[float, float]:
        r = math.radians(self.twist)
        dx, dy = x - self.tx, y - self.ty
        u, v = dx * math.cos(r) - dy * math.sin(r), dx * math.sin(r) + dy * math.cos(r)
        k = self.h / self.vh
        return self.cx + (u - self.vx) * k, self.cy + (v - self.vy) * k


@dataclass
class Drawing:
    spaces: list = field(default_factory=list)      # 空间名，模型空间在前，布局按文件里的顺序
    texts: list = field(default_factory=list)       # [Text]
    inserts: list = field(default_factory=list)     # [Insert]
    viewports: list = field(default_factory=list)   # [Viewport]
    blocks: dict = field(default_factory=dict)      # 块名 → 外框 (x0, y0, x1, y1)，按块里的线算
    inner: dict = field(default_factory=dict)       # 块名 → 块里套着的带属性的块参照 [Insert]（坐标是块里的）
    xrefs: set = field(default_factory=set)         # 外部参照的块名（附着的，和绑定进来的）
    version: str = ""
    reader: str = ""


# ---------------- 找 LibreDWG ----------------

def find_tool() -> str | None:
    """dwgread 在哪：环境变量 BIDBENCH_DWGREAD > 安装包里附带的 > 系统里装的。"""
    env = os.environ.get("BIDBENCH_DWGREAD")
    if env:
        return env if Path(env).exists() else None
    exe = "dwgread.exe" if os.name == "nt" else "dwgread"
    for b in (Path(getattr(sys, "_MEIPASS", "") or "."), Path(sys.executable).parent):
        if (b / "libredwg" / exe).exists():
            return str(b / "libredwg" / exe)
    return shutil.which("dwgread")


# ---------------- 读 DWG（LibreDWG） ----------------

WANT = {"BLOCK_HEADER", "LAYOUT", "TEXT", "MTEXT", "INSERT", "MINSERT", "ATTRIB", "LINE", "LWPOLYLINE", "VIEWPORT", "SEQEND"}
KIND_LINE = re.compile(r'^\s*"(?:entity|object)": "([A-Z0-9_]+)"')


def iter_json(lines):
    """dwgread -O JSON 的输出逐行读：返回 ({"version", "reader"}, 只含要用的对象的迭代器)。
    输出是排好版的：OBJECTS 里每个对象从一行“    {”开始，到“    }”或“    },”结束。"""
    meta = {"version": "", "reader": ""}

    def objects():
        inside, buf, skip = False, None, False
        for raw in lines:
            line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).rstrip("\r\n")
            if not inside:
                if line.startswith('  "OBJECTS": ['):
                    inside = True
                elif line.startswith('  "created_by":'):
                    meta["reader"] = line.split(":", 1)[1].strip().strip('",')
                elif line.startswith('    "version":') and not meta["version"]:
                    meta["version"] = line.split(":", 1)[1].strip().strip('",')
                continue
            if line.startswith("  ]"):
                inside = False
                continue
            if line == "    {":
                buf, skip = [line], False
                continue
            if buf is None:
                continue
            if line in ("    }", "    },"):
                if not skip:
                    try:
                        yield json.loads("\n".join(buf) + "\n    }")
                    except ValueError:
                        pass  # 坏了一个对象不影响别的
                buf = None
                continue
            if skip:
                continue
            if len(buf) == 1:
                m = KIND_LINE.match(line)
                if not m or m.group(1) not in WANT:
                    skip = True
                    continue
            buf.append(line)

    return meta, objects()


def _dwgread(path: Path, tool: str | None, consume):
    """dwgread -O JSON 边读边交给 consume(objs, meta)，超时就停；返回 (consume 的结果, meta)。"""
    tool = tool or find_tool()
    if not tool:
        raise ReadError("没有找到 LibreDWG（dwgread），只能按文件名暂定")
    cmd = [tool, "-O", "JSON", str(path)]
    log.debug("读 DWG：%s", debuglog.safe_command(cmd))
    limit = timeout_for(path.stat().st_size)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **proc.NO_WINDOW)
    except OSError as e:
        raise ReadError(f"LibreDWG 启动不了：{e}") from None
    killed = threading.Event()

    def kill():
        killed.set()
        p.kill()

    timer = threading.Timer(limit, kill)
    timer.start()
    try:
        meta, objs = iter_json(p.stdout)
        out = consume(objs, meta)
        p.wait()
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
    log.debug("dwgread 退出码 %s", p.returncode)
    if killed.is_set():
        raise ReadError(f"读了 {limit} 秒还没读完，跳过")
    if not meta["reader"]:
        raise ReadError("LibreDWG 读不出这个文件（可能损坏，或版本太新）")
    return out, meta


def read_dwg(path: Path, tool: str | None = None) -> Drawing:
    d, _meta = _dwgread(path, tool, from_objects)
    log.debug("%s 段文字，%s 个块参照", len(d.texts), len(d.inserts))
    return d


def insert_blocks(path: Path, tool: str | None = None) -> dict[int, int]:
    """每个块参照（句柄）用的是哪个块（块记录的句柄）：dwg2dxf 会把动态块的匿名块名写成光秃秃的“*U”，
    dwgread 读出的块名也是“*U”，只有句柄靠得住，名字到 DXF 自己的块记录表里找（cadfile.fix_anonymous_blocks）。"""
    def consume(objs, _meta):
        refs: dict[int, int] = {}
        for o in objs:
            kind = o.get("entity") or o.get("object")
            h = _ref(o.get("handle"))
            if kind in ("INSERT", "MINSERT") and h is not None and _ref(o.get("block_header")) is not None:
                refs[h] = _ref(o.get("block_header"))
        return refs
    return _dwgread(path, tool, consume)[0]


U_ESC = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def _s(v) -> str:
    """LibreDWG、老版本 DXF 里的非 ASCII 字写成 \\U+XXXX。"""
    if not isinstance(v, str):
        return ""
    return U_ESC.sub(lambda m: chr(int(m.group(1), 16)), v)


def _ref(v) -> int | None:
    """句柄引用 [code, size, value, absolute] 取绝对句柄；对象自己的句柄 [code, size, value] 取 value。"""
    if isinstance(v, list) and v and all(isinstance(x, int) for x in v):
        return v[3] if len(v) >= 4 else v[-1]
    return v if isinstance(v, int) else None


def _pt(v) -> tuple[float, float]:
    if isinstance(v, list) and len(v) >= 2 and all(isinstance(x, (int, float)) for x in v[:2]):
        return float(v[0]), float(v[1])
    return 0.0, 0.0


def _num(v, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else default


def from_json(data: dict) -> Drawing:
    """整个 JSON（测试用）→ Drawing。"""
    return from_objects(data.get("OBJECTS") or [], {"version": str((data.get("FILEHEADER") or {}).get("version") or ""),
                                                    "reader": str(data.get("created_by") or "")})


def from_objects(objs, meta: dict) -> Drawing:
    """LibreDWG 的对象 → Drawing。块定义、布局可能出现在实体后面，所以实体先记下，最后再归到空间。"""
    bh_name: dict[int, str] = {}       # 块记录 → 名字（*Model_Space、*Paper_Space0、图签块……）
    layout_of: dict[int, str] = {}     # 布局的块记录 → 布局名
    layout_order: dict[int, int] = {}  # 布局的块记录 → 排第几
    raw = []                           # (类型, 对象)
    xref_names: set[str] = set()
    for o in objs:
        kind = o.get("entity") or o.get("object")
        if kind == "BLOCK_HEADER":
            h = _ref(o.get("handle"))
            if h is not None:
                bh_name[h] = _s(o.get("name"))
            if _s(o.get("xref_pname")).strip():  # 附着的外部参照：块记录里有参照文件的路径
                xref_names.add(_s(o.get("name")))
        elif kind == "LAYOUT":
            bh = _ref(o.get("block_header")) or _ref(o.get("pspace_block_header"))
            if bh is not None:
                layout_of[bh] = _s(o.get("layout_name"))
                layout_order[bh] = int(_num(o.get("tab_order"), len(layout_order) + 1))
        elif o.get("entity"):
            raw.append((kind, o))
    active_ps = next((h for h, n in bh_name.items() if n.upper() == "*PAPER_SPACE"), None)

    def where(o: dict) -> tuple[str | None, str | None]:
        """(空间名, 块名)：实体在模型或布局里返回空间名，在块定义里返回块名。
        entmode 2 是模型空间，1 是当前布局，0 看 ownerhandle（块定义或别的布局）。"""
        mode = o.get("entmode")
        if mode == 2:
            return MODEL, None
        owner = active_ps if mode == 1 else _ref(o.get("ownerhandle"))
        name = bh_name.get(owner) if owner is not None else None
        if name is None:
            return None, None
        up = name.upper()
        if up == "*MODEL_SPACE":
            return MODEL, None
        if up.startswith("*PAPER_SPACE"):
            lay = layout_of.get(owner) or name
            return (MODEL if lay.upper() == "MODEL" else lay), None
        return None, name

    d = Drawing(version=meta.get("version", ""), reader=meta.get("reader", ""))
    lines: dict[str, list] = {}
    inserts: dict[int, Insert] = {}
    by_attr: dict[int, int] = {}
    last: Insert | None = None
    for kind, o in raw:
        h = _ref(o.get("handle"))
        space, block = where(o)
        if kind == "LINE":
            if block:
                lines.setdefault(block, []).extend([_pt(o.get("start")), _pt(o.get("end"))])
        elif kind == "LWPOLYLINE":
            if block:
                lines.setdefault(block, []).extend(_pt(p) for p in o.get("points") or [])
        elif kind in ("TEXT", "MTEXT"):
            if space:
                t = _s(o.get("text_value") if kind == "TEXT" else o.get("text"))
                t = clean_mtext(t) if kind == "MTEXT" else t
                x, y = _pt(o.get("ins_pt"))
                if t.strip():
                    d.texts.append(Text(t.strip(), x, y, space, _num(o.get("height") or o.get("text_height"))))
        elif kind == "VIEWPORT":
            if space and space != MODEL:
                cx, cy = _pt(o.get("center"))
                vx, vy = _pt(o.get("VIEWCTR") or o.get("view_center"))
                w, hh, vh = _num(o.get("width")), _num(o.get("height")), _num(o.get("VIEWSIZE") or o.get("view_height"))
                paper_itself = abs(cx - vx) < 1e-6 and abs(cy - vy) < 1e-6 and abs(hh - vh) < 1e-6  # 布局本身那个视口
                if w > 0 and hh > 0 and vh > 0 and not paper_itself:
                    tx, ty = _pt(o.get("view_target"))
                    twist = _num(o.get("twist_angle"))
                    d.viewports.append(Viewport(space, cx, cy, w, hh, vx, vy, vh, math.degrees(twist), tx, ty))
        elif kind in ("INSERT", "MINSERT"):
            last = None
            if space or block:
                sc = o.get("scale") if isinstance(o.get("scale"), list) and len(o.get("scale")) >= 2 else [1, 1]
                x, y = _pt(o.get("ins_pt"))
                ins = Insert(bh_name.get(_ref(o.get("block_header")), ""), x, y, _num(sc[0], 1) or 1, _num(sc[1], 1) or 1,
                             _num(o.get("rotation")), space or "", nested=not space)
                if not space:  # 块里套的块：用它的地方再摆出来
                    d.inner.setdefault(block, []).append(ins)
                f, la = _ref(o.get("first_attrib")), _ref(o.get("last_attrib"))
                ins.rng = (f, la) if f and la else None
                inserts[h] = last = ins
                for a in o.get("attribs") or []:  # R2004 以后：属性句柄列表
                    if _ref(a) is not None:
                        by_attr[_ref(a)] = h
        elif kind == "ATTRIB":
            ins = inserts.get(by_attr.get(h))
            if ins is None and last is not None and (last.rng is None or last.rng[0] <= (h or 0) <= last.rng[1]):
                ins = last
            if ins is not None:
                x, y = _pt(o.get("ins_pt"))
                ins.attrs.append((_s(o.get("tag")), _s(o.get("text_value")).strip(), x, y))
        elif kind != "SEQEND":
            last = None
    d.inserts = [i for i in inserts.values() if not i.nested]
    d.inner = {k: [i for i in v if i.attrs] for k, v in d.inner.items() if any(i.attrs for i in v)}
    _expand(d)
    d.blocks = {k: _bbox(v) for k, v in lines.items() if v}
    d.xrefs = _xref_blocks(xref_names, bh_name.values())
    order = sorted(layout_of, key=lambda b: (layout_order.get(b, 99), b))
    d.spaces = [MODEL] + [n for n in dict.fromkeys(layout_of[b] for b in order) if n and n.upper() != "MODEL"]
    for x in (*d.texts, *d.inserts):
        if x.space not in d.spaces:
            d.spaces.append(x.space)
    return d


def _bbox(pts) -> tuple | None:
    pts = [p for p in pts if all(math.isfinite(c) for c in p)]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


MTEXT_CODES = [
    (re.compile(r"\\[Pp]"), "\n"),
    (re.compile(r"\\[ACcFfHhQqTtWw][^;\\]*;"), ""),
    (re.compile(r"\\S([^;]*?)[\^/#]([^;]*);"), r"\1/\2"),
    (re.compile(r"\\[LlOoKk]"), ""),
    (re.compile(r"\\~"), " "),
    (re.compile(r"[{}]"), ""),
]


def clean_mtext(t: str) -> str:
    """多行文字去掉格式代码：{\\fSimSun|b0;文字}、\\P 换行等。"""
    t = t.replace("\\\\", "\x00").replace("\\{", "\x01").replace("\\}", "\x02")
    for rx, rep in MTEXT_CODES:
        t = rx.sub(rep, t)
    return t.replace("\x00", "\\").replace("\x01", "{").replace("\x02", "}")


# ---------------- 读 DXF（自己读） ----------------

def read_dxf(path: Path) -> Drawing:
    raw = path.read_bytes()
    if raw[:22] == b"AutoCAD Binary DXF\r\n\x1a\x00":
        raise ReadError("二进制 DXF 还读不了，只能按文件名暂定")
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    pairs = [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]
    try:
        return from_dxf_pairs(pairs)
    except Exception as e:  # noqa: BLE001 - 读不出就按文件名暂定
        raise ReadError(f"DXF 读不出：{type(e).__name__}") from None


def _getter(g):
    def get(code):
        for c, v in g:
            if c == code:
                return v.strip()
        return None
    return get


def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def from_dxf_pairs(pairs: list[tuple[str, str]]) -> Drawing:
    d = Drawing(reader="bidbench DXF")
    ents: list[list] = []  # 每个实体是 [所在段, 类型, [(组码, 值)…]]
    section = None
    for code, val in pairs:
        if code == "0":
            v = val.strip()
            if v == "SECTION":
                section = "?"
            elif v == "ENDSEC":
                section = None
            ents.append([section, v, []])
        elif ents:
            if code == "2" and ents[-1][1] == "SECTION" and ents[-1][0] == "?":
                ents[-1][0] = section = val.strip()
            ents[-1][2].append((code, val))
    layout_by_block: dict[str, str] = {}  # 布局的块记录句柄 → 布局名
    order: dict[str, int] = {}
    for section, kind, g in ents:
        if section == "OBJECTS" and kind == "LAYOUT":
            sub = next((i for i, (c, v) in enumerate(g) if c == "100" and v.strip() == "AcDbLayout"), None)
            if sub is None:
                continue
            get = _getter(g[sub:])  # 布局名和布局的块记录句柄在 AcDbLayout 后面
            if get("1") and get("330"):
                layout_by_block[get("330").upper()] = _s(get("1"))
                order[_s(get("1"))] = int(_f(get("71"), 99))
    lines: dict[str, list] = {}
    xref_names: set[str] = set()
    block_names: list[str] = []
    block = ps_space = None  # 正在读的块定义 / 布局块（非当前布局的内容放在 *Paper_Space0 这类块里）
    vps: list[tuple] = []     # (编号, 视口)
    last: Insert | None = None
    for section, kind, g in ents:
        get = _getter(g)
        if section == "HEADER":
            for i, (c, v) in enumerate(g):
                if c == "9" and v.strip() == "$ACADVER" and i + 1 < len(g):
                    d.version = g[i + 1][1].strip()
            continue
        if section == "BLOCKS" and kind in ("BLOCK", "ENDBLK"):
            block = ps_space = last = None
            if kind == "BLOCK":
                name = _s(get("2") or "")
                if name.upper().startswith("*PAPER_SPACE"):
                    ps_space = layout_by_block.get((get("330") or "").upper()) or "布局"
                elif not name.upper().startswith("*MODEL_SPACE"):
                    block = name
                    if (get("1") or "").strip() or int(_f(get("70"))) & 4:  # 附着的外部参照
                        xref_names.add(name)
                block_names.append(name)
            continue
        if section == "BLOCKS" and block:
            if kind == "INSERT":
                last = Insert(_s(get("2") or ""), _f(get("10")), _f(get("20")), _f(get("41"), 1.0), _f(get("42"), 1.0),
                              math.radians(_f(get("50"))), "", nested=True)
                d.inner.setdefault(block, []).append(last)
                continue
            if kind == "ATTRIB" and last is not None:
                last.attrs.append((_s(get("2") or ""), _s(get("1") or "").strip(), _f(get("10")), _f(get("20"))))
                continue
            if kind != "SEQEND":
                last = None
            if kind == "LINE":
                lines.setdefault(block, []).extend([(_f(get("10")), _f(get("20"))), (_f(get("11")), _f(get("21")))])
            elif kind == "LWPOLYLINE":
                lines.setdefault(block, []).extend(zip([_f(v) for c, v in g if c == "10"], [_f(v) for c, v in g if c == "20"]))
            elif kind == "VERTEX":
                lines.setdefault(block, []).append((_f(get("10")), _f(get("20"))))
            continue
        if section == "ENTITIES":
            space = (_s(get("410") or "") or "布局") if get("67") == "1" else MODEL
        elif section == "BLOCKS" and ps_space:
            space = ps_space
        else:
            continue
        space = MODEL if space.upper() == "MODEL" else space
        if kind in ("TEXT", "MTEXT"):
            t = _s(get("1") or "")
            if kind == "MTEXT":
                t = clean_mtext(_s("".join(v for c, v in g if c == "3")) + t)
            t = re.sub(r"%%[cC]", "Ø", t).replace("%%d", "°").replace("%%p", "±").strip()
            if t:
                d.texts.append(Text(t, _f(get("10")), _f(get("20")), space, _f(get("40"))))
            last = None
        elif kind == "VIEWPORT":
            if space != MODEL:
                vp = Viewport(space, _f(get("10")), _f(get("20")), _f(get("40")), _f(get("41")),
                              _f(get("12")), _f(get("22")), _f(get("45")), _f(get("51")), _f(get("17")), _f(get("27")))
                if vp.w > 0 and vp.h > 0 and vp.vh > 0:
                    vps.append((get("69"), vp))
            last = None
        elif kind == "INSERT":
            last = Insert(_s(get("2") or ""), _f(get("10")), _f(get("20")), _f(get("41"), 1.0), _f(get("42"), 1.0),
                          math.radians(_f(get("50"))), space)
            d.inserts.append(last)
        elif kind == "ATTRIB" and last is not None:
            last.attrs.append((_s(get("2") or ""), _s(get("1") or "").strip(), _f(get("10")), _f(get("20"))))
        elif kind != "SEQEND":
            last = None
    d.viewports = _floating(vps)
    d.inner = {k: [i for i in v if i.attrs] for k, v in d.inner.items() if any(i.attrs for i in v)}
    _expand(d)
    d.blocks = {k: _bbox(v) for k, v in lines.items() if v}
    d.xrefs = _xref_blocks(xref_names, block_names)
    d.spaces = [MODEL] + sorted((n for n in dict.fromkeys(layout_by_block.values()) if n.upper() != "MODEL"),
                                key=lambda n: order.get(n, 99))
    for x in (*d.texts, *d.inserts):
        if x.space not in d.spaces:
            d.spaces.append(x.space)
    return d


def _place(parent: Insert, x: float, y: float) -> tuple[float, float]:
    c, s_ = math.cos(parent.rot), math.sin(parent.rot)
    lx, ly = x * parent.sx, y * parent.sy
    return parent.x + lx * c - ly * s_, parent.y + lx * s_ + ly * c


def _expand(d: Drawing, depth: int = 4) -> None:
    """块里套的带属性的块参照（动态块里的轴号圈、索引符号）摆到用它的地方，属性当成图上的字读。"""
    todo = [i for i in d.inserts if d.inner.get(i.block)]
    for _ in range(depth):
        nxt = []
        for parent in todo:
            for child in d.inner.get(parent.block, ()):
                x, y = _place(parent, child.x, child.y)
                ins = Insert(child.block, x, y, child.sx * parent.sx, child.sy * parent.sy, child.rot + parent.rot,
                             parent.space, [(t, v, *_place(parent, ax, ay)) for t, v, ax, ay in child.attrs], nested=True)
                d.inserts.append(ins)
                if d.inner.get(ins.block):
                    nxt.append(ins)
        todo = nxt
        if not todo:
            break


def _floating(vps: list[tuple]) -> list[Viewport]:
    """去掉布局本身那个视口：看到的就是图纸本身（中心、高和布局上一样）；认不出时去掉编号 1 的。
    AutoCAD 只给布局本身编号 1，dwg2dxf 转出的 DXF 里浮动视口也写成 1，所以不能只看编号。"""
    def itself(v: Viewport) -> bool:
        return abs(v.cx - v.vx) < 1e-6 and abs(v.cy - v.vy) < 1e-6 and abs(v.h - v.vh) < 1e-6 and not v.twist
    out = []
    for space in dict.fromkeys(v.space for _n, v in vps):
        own = [(n, v) for n, v in vps if v.space == space]
        if any(itself(v) for _n, v in own):
            out += [v for _n, v in own if not itself(v)]
        elif len({n for n, _v in own}) > 1:
            out += [v for n, v in own if n != "1"]
        else:  # 编号都一样（dwg2dxf）：第一个是布局本身
            out += [v for _n, v in own[1:]] if len(own) > 1 else [v for n, v in own if n != "1"]
    return out


def read(path: Path, ext: str | None = None, tool: str | None = None) -> Drawing:
    return read_dxf(path) if (ext or path.suffix).lower() == ".dxf" else read_dwg(path, tool)


# ---------------- 认图签 ----------------

# 属性名（去掉空格、下划线等，大写）→ 图签的哪一项；JUDUL、SKALA、TANGGAL 等是印尼语
TAGS = {
    "drawing_no": ("图号", "图纸编号", "图纸号", "图别图号", "DWGNO", "DRGNO", "DRAWINGNO", "DRAWINGNUMBER", "DRAWINGNUM",
                   "SHEETNO", "SHEETNUMBER", "SHEETNUM", "DOCNO", "DOCUMENTNO", "DOCUMENTNUMBER", "NOGAMBAR", "NOMORGAMBAR",
                   "NOGBR", "NOGB"),
    "drawing_title": ("图名", "图纸名称", "图纸名", "图样名称", "DWGTITLE", "DRAWINGTITLE", "DRAWINGNAME", "SHEETTITLE",
                      "SHEETNAME", "TITLE", "JUDUL", "JUDULGAMBAR"),
    "revision": ("版次", "版本", "版本号", "修改版次", "REV", "REVISION", "REVNO", "REVISIONNO", "CURRENTREVISION", "REVISI"),
    "issue_date": ("日期", "出图日期", "DATE", "ISSUEDATE", "DRAWINGDATE", "TANGGAL", "TGL"),
    "scale": ("比例", "SCALE", "SKALA"),
    "project_name": ("工程名称", "项目名称", "工程名", "PROJECT", "PROJECTNAME", "PROJECTTITLE", "PROYEK"),
    "designer_org": ("设计单位", "设计院", "DESIGNER", "COMPANY", "CONSULTANT"),
    "discipline": ("专业", "DISCIPLINE"),
    "subitem": ("子项", "子项名称", "单体", "单体名称", "子项工程"),
}
TAG_ROLE = {t: role for role, tags in TAGS.items() for t in tags}
MULTI_LINE = ("drawing_title", "project_name")  # 分几行写的：图名1、JUDUL2


def tag_role(tag: str) -> tuple[str | None, int]:
    """属性名属于图签的哪一项：(字段, 第几行)。"""
    t = re.sub(r"[\s_\-.:：()（）]+", "", tag or "").upper()
    if t in TAG_ROLE:
        return TAG_ROLE[t], 0
    m = re.match(r"^(.*?)(\d{1,2})$", t)
    if m and TAG_ROLE.get(m.group(1)) in MULTI_LINE:
        return TAG_ROLE[m.group(1)], int(m.group(2))
    return None, 0


NO_CELL = re.compile(r"^(?:[一-鿿]{1,2}施\s*[-－—]?\s*\d{1,3}[A-Za-z]?|[A-Z]{1,3}[-－—]\d{2,4}[A-Z]?|"
                     r"[A-Z0-9]{1,8}(?:-[A-Z0-9]{1,8}){2,9})$")


def _norm_no(s: str) -> str:
    return re.sub(r"\s+", "", s).replace("－", "-").replace("—", "-")


def looks_like_no(v: str) -> bool:
    """像图号：建施-01、A-101、LDI-A-224、PRJ1-ARC-ZZ-00-DR-A-1001。纯数字、日期、比例不算。"""
    v = _norm_no(v or "")
    return (bool(v) and len(v) <= 60 and bool(NO_CELL.match(v)) and bool(re.search(r"[A-Za-z一-鿿]", v))
            and bool(re.search(r"\d", v)))


def title_fields(attrs: list) -> tuple[dict[str, str], dict[str, float]]:
    """一个图签块的属性 → ({字段: 值}, {字段: 把握分数})。
    图号的属性名认不出（各家图签自己编的属性名，如 KD0105）时，属性值像图号的只有一个，就用它（把握中）。"""
    parts: dict[str, list] = {}
    for tag, val, _x, _y in attrs:
        role, line = tag_role(tag)
        if role and val:
            parts.setdefault(role, []).append((line, val))
    out = {k: " ".join(v for _n, v in sorted(vs, key=lambda x: x[0])) for k, vs in parts.items()}
    conf = {k: ATTR_CONF for k in out}
    if "drawing_no" not in out and len(attrs) >= 3:
        cands = list(dict.fromkeys(v for t, v, _x, _y in attrs if v and not tag_role(t)[0] and looks_like_no(v)))
        if len(cands) == 1:
            out["drawing_no"], conf["drawing_no"] = cands[0], NEAR_CONF
    return out, conf


def is_title_block(ins: Insert) -> bool:
    return not ins.nested and "drawing_no" in title_fields(ins.attrs)[0]


# 图签是普通文字时（Revit 导出的图常这样）：标签右边或下面的字就是值
LABELS = {
    "drawing_no": re.compile(r"^(?:sheet\s*(?:no|number)|drawing\s*(?:no|number)|dwg\.?\s*no|drg\.?\s*no|图号|图纸编号)\.?\s*[:：]?\s*", re.I),
    "drawing_title": re.compile(r"^(?:sheet\s*(?:title|name)|drawing\s*(?:title|name)|图名|图纸名称)\.?\s*[:：]?\s*", re.I),
    "revision": re.compile(r"^(?:rev(?:ision)?|版次)\.?\s*[:：]?\s*", re.I),
    "issue_date": re.compile(r"^(?:date|issue\s*date|出图日期|日期)\.?\s*[:：]?\s*", re.I),
    "scale": re.compile(r"^(?:scale|比例)\.?\s*[:：]?\s*", re.I),
}


def _is_label(t: str) -> bool:
    return any(rx.match(t) and not rx.sub("", t, count=1).strip() for rx in LABELS.values())


def label_fields(texts: list) -> tuple[dict[str, str], dict[str, float]]:
    """按位置配对图签文字：“Sheet No.”右边或下面最近的字。“图号：A-01”这种写在一起的也认。"""
    out: dict[str, str] = {}
    for role, rx in LABELS.items():
        best = None
        for lab in texts:
            m = rx.match(lab.text)
            if not m:
                continue
            rest = lab.text[m.end():].strip()
            if rest:
                if ((role != "drawing_no" or looks_like_no(rest)) and (role != "revision" or REV_CELL.match(rest))
                        and len(rest) <= 120):
                    best = (0.0, rest)
                    break
                continue
            h = lab.h or 2.5
            for t in texts:
                if t is lab or _is_label(t.text) or len(t.text) > 120:
                    continue
                dx, dy = t.x - lab.x, lab.y - t.y
                if abs(dy) <= h * 0.8 and 0 < dx <= h * 60:
                    dist = dx
                elif abs(dx) <= h * 30 and 0 < dy <= h * 4:
                    dist = dy * 2
                else:
                    continue
                if role == "drawing_no" and not looks_like_no(t.text):
                    continue
                if role == "revision" and not REV_CELL.match(t.text.strip()):
                    continue  # 修改记录表头“REV | DESCRIPTION | DATE”：右边是别的表头，版次在下面
                if best is None or dist < best[0]:
                    best = (dist, t.text)
        if best:
            out[role] = best[1]
    return out, {k: NEAR_CONF for k in out}


# ---------------- 分图 ----------------

@dataclass
class Sheet:
    space: str
    fields: dict                # 认出的图签字段
    conf: dict                  # 每项的把握分数
    attrs: list                 # 图签块的全部属性 [(属性名, 值)]
    texts: list                 # 归这张图的文字 [Text]
    box: tuple | None = None    # 这张图的范围（图签外框，或布局上内容的范围），算文字在图上的相对位置用
    how: str = ""               # 图号是怎么认出来的
    xrefs: list = field(default_factory=list)  # 这张图用到的外部参照名


def _insert_box(ins: Insert, blocks: dict) -> tuple | None:
    b = blocks.get(ins.block)
    if not b:
        return None
    turns = round(ins.rot / (math.pi / 2)) % 4
    if abs(ins.rot - round(ins.rot / (math.pi / 2)) * math.pi / 2) > 1e-3 or turns % 2:
        return None  # 转了 90° 这类的外框不好算，按最近的图签分
    sx, sy = (ins.sx, ins.sy) if turns == 0 else (-ins.sx, -ins.sy)
    xs = (ins.x + b[0] * sx, ins.x + b[2] * sx)
    ys = (ins.y + b[1] * sy, ins.y + b[3] * sy)
    return min(xs), min(ys), max(xs), max(ys)


def _inside(box, x, y) -> bool:
    return box is not None and box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _dist(box, x, y, ins: Insert) -> float:
    if box is None:
        return math.hypot(x - ins.x, y - ins.y)
    return math.hypot(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))


def _merge_title_parts(d: Drawing, space: str, titles: list, tb: list, boxes: list, group: list) -> None:
    """图签拆成几个块的（一个块写图号、图名，另一个“状态”块写版次、日期）：别的块里认得出的图签项补到这张图上，
    放在哪张图的图签外框里就归哪张；这个空间只有一张图的归它。已有的项不覆盖。"""
    for i in d.inserts:
        if i.space != space or i.nested or i in titles:
            continue
        f, c = title_fields(i.attrs)
        f.pop("drawing_no", None)
        if not f:
            continue
        hit = [n for n, b in enumerate(boxes) if _inside(b, i.x, i.y)]
        n = hit[0] if len(hit) == 1 else 0 if len(tb) == 1 else None
        if n is None:
            continue
        added = {k for k in f if k not in group[n].fields}
        for k in added:
            group[n].fields[k], group[n].conf[k] = f[k], c[k]
        group[n].attrs += [(t, v) for t, v, _x, _y in i.attrs if v and tag_role(t)[0] in added]


def split_sheets(d: Drawing) -> list[Sheet]:
    """一个文件分成几张图（见本模块开头）。"""
    titles = [i for i in d.inserts if is_title_block(i)]
    texts = list(d.texts)
    for i in d.inserts:  # 不是图签的块属性（详图索引符号、房间标签……）也当文字，标成“索引”
        if i not in titles:
            texts += [Text(v, x, y, i.space, tag=t, callout=True, mark=_mark(i.attrs, v)) for t, v, x, y in i.attrs if v]
    order = {s: n for n, s in enumerate(d.spaces)}
    sheets: list[Sheet] = []
    for sp in d.spaces:
        tb = sorted((i for i in titles if i.space == sp), key=lambda i: (-round(i.y, -2), i.x))  # 一排排从左到右
        own = [t for t in texts if t.space == sp]
        if tb:
            boxes = [_insert_box(i, d.blocks) for i in tb]
            group = []
            for i, b in zip(tb, boxes):
                f, c = title_fields(i.attrs)
                group.append(Sheet(sp, f, c, [(t, v) for t, v, _x, _y in i.attrs], [], b,
                                   "图签属性" if c.get("drawing_no") == ATTR_CONF else "图签属性的值"))
            _merge_title_parts(d, sp, titles, tb, boxes, group)
            for t in own:
                inside = [n for n, b in enumerate(boxes) if _inside(b, t.x, t.y)]
                if inside:
                    n = min(inside, key=lambda n: (boxes[n][2] - boxes[n][0]) * (boxes[n][3] - boxes[n][1]))
                else:
                    n = min(range(len(tb)), key=lambda n: _dist(boxes[n], t.x, t.y, tb[n]))
                group[n].texts.append(t)
            sheets += group
        elif sp != MODEL and (own or any(v.space == sp for v in d.viewports)):
            f, c = label_fields(own)
            how = "图签文字" if f.get("drawing_no") else ""
            if not f.get("drawing_no") and looks_like_no(sp):
                f["drawing_no"], c["drawing_no"], how = sp, NEAR_CONF, "布局名"
            pts = [(t.x, t.y) for t in own] + [p for v in d.viewports if v.space == sp
                                               for p in ((v.cx - v.w / 2, v.cy - v.h / 2), (v.cx + v.w / 2, v.cy + v.h / 2))]
            sheets.append(Sheet(sp, f, c, [], list(own), _bbox(pts), how))
    # 模型空间里的文字：按布局视口看到的范围归到布局那张图（模型空间自己有图签的，这些字照样留在模型空间那张图上）；
    # 模型空间没有图签块时，没有视口、只有一个布局图的全归它；没有布局图的，模型空间自己算一张
    model_texts = [t for t in texts if t.space == MODEL]
    has_model = any(s.space == MODEL for s in sheets)
    layout_sheets = {s.space: s for s in sheets if s.space != MODEL}
    vps = [v for v in d.viewports if v.space in layout_sheets]
    if model_texts and vps:
        for t in model_texts:
            for v in vps:
                if _inside(v.window(), t.x, t.y):
                    px, py = v.to_paper(t.x, t.y)
                    layout_sheets[v.space].texts.append(replace(t, x=px, y=py, space=v.space))
    elif model_texts and not has_model and len(layout_sheets) == 1:
        s = next(iter(layout_sheets.values()))
        s.texts += [replace(t, space=s.space) for t in model_texts]
        s.box = None  # 模型和布局的坐标不一样，算不出相对位置
    elif model_texts and not has_model and not layout_sheets:
        f, c = label_fields(model_texts)
        sheets.append(Sheet(MODEL, f, c, [], model_texts, _bbox([(t.x, t.y) for t in model_texts]),
                            "图签文字" if f.get("drawing_no") else ""))
    sheets.sort(key=lambda s: order.get(s.space, 99))  # 稳定排序：同一空间里保持一排排的顺序
    if any(s.fields.get("drawing_no") for s in sheets):  # 别的布局都认出了图号：没几个字、也没有视口的空布局不算一张图
        sheets = [s for s in sheets if s.fields.get("drawing_no") or len(s.texts) >= 3
                  or any(v.space == s.space for v in d.viewports)]
    _assign_xrefs(d, sheets)
    return sheets


BOUND = re.compile(r"^(.+?)\$\d+\$")  # 绑定进来的外部参照，里面的块、图层名写成“参照名$0$原名”


def _xref_blocks(attached: set, names) -> set:
    """外部参照的块名：附着的（块记录里有路径），加上绑定进来的（别的块名是“它$0$……”）。"""
    names = list(names)
    found = set(attached)
    top = {n for n in names if not BOUND.match(n)}
    for n in names:
        m = BOUND.match(n)
        if m and m.group(1) in top:
            found.add(m.group(1))
    return {n for n in found if n and not n.startswith("*")}


def _assign_xrefs(d: Drawing, sheets: list) -> None:
    """外部参照归到图上：和图在同一空间（同一空间有几张图的，按参照范围和图框有没有重叠）；
    模型空间里的参照，布局视口看得到的归那个布局的图；只有一张布局图的全归它。"""
    xs = [i for i in d.inserts if i.block in d.xrefs]
    for i in xs:
        box = _insert_box(i, d.blocks) or (i.x, i.y, i.x, i.y)
        same = [s for s in sheets if s.space == i.space]
        hit = [s for s in same if len(same) == 1 or s.box is None or _overlap(s.box, box)]
        if not same and i.space == MODEL:
            layouts = [s for s in sheets if s.space != MODEL]
            vps = [v for v in d.viewports if any(s.space == v.space for s in layouts)]
            seen = {v.space for v in vps if _overlap(v.window(), box)}
            hit = [s for s in layouts if s.space in seen] if vps else layouts if len(layouts) == 1 else []
        for s in hit:
            if i.block not in s.xrefs:
                s.xrefs.append(i.block)


def _overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


MARK = re.compile(r"^[0-9A-Za-z]{1,3}(?:-[0-9A-Za-z]{1,3})?$")


def _mark(attrs: list, value: str) -> str | None:
    """索引符号的详图编号：同一个块里另一个短编号（5、1-1），不是图号。"""
    for _t, v, _x, _y in attrs:
        if v and v != value and MARK.match(v) and not looks_like_no(v):
            return v
    return None


# ---------------- 引用、目录行 ----------------

CN_REF = re.compile(r"([建结水暖电给排风动幕景装总强弱消智园][一-鿿]?施)\s*[-－—]?\s*(\d{1,3}[A-Za-z]?)(?!\d)")
SIMPLE_REF = re.compile(r"(?<![A-Za-z0-9-])([A-Z]{1,3})[-－—](\d{2,4}[A-Z]?)(?![\w-])")
SEE_SIMPLE = re.compile(r"(?:详见|参见|见|索引|see|refer\s*to)\s*(?:图\s*|drawings?\s*)?([A-Z]{1,3})[-－—](\d{2,4}[A-Z]?)(?![\w-])", re.I)
REV_CELL = re.compile(r"^(?:[PCT]\d{2,3}|[A-Z]\d?|第\d+版|\d{1,2})$")


def find_numbers(text: str, callout: bool = False) -> list[str]:
    """一段文字里写的图号：ISO 写法、“建施-12”写法；“A-101”这种短写法只在紧跟“详见”“REFER TO”之类的词、
    或在索引符号里才算（不然门窗编号、构件编号也像图号）。"""
    out = []
    for m in names.ISO_NO.finditer(text.upper()):
        if names.iso_parts(m.group(1)):
            out.append(m.group(1))
    for m in CN_REF.finditer(text):
        out.append(f"{m.group(1)}-{m.group(2)}")
    for m in (SIMPLE_REF.finditer(text.upper()) if callout else SEE_SIMPLE.finditer(text)):
        out.append(f"{m.group(1).upper()}-{m.group(2).upper()}")
    return list(dict.fromkeys(out))


def _rel_box(box, x, y, w=0.02) -> list | None:
    """图纸坐标 → 页面上的相对位置（0–1，原点左上角），给“跳到图上的位置”用。"""
    if box is None or box[2] <= box[0] or box[3] <= box[1]:
        return None
    u = min(max((x - box[0]) / (box[2] - box[0]), 0), 1)
    v = min(max(1 - (y - box[1]) / (box[3] - box[1]), 0), 1)
    return [round(max(u - w / 2, 0), 4), round(max(v - w / 2, 0), 4), round(min(u + w / 2, 1), 4), round(min(v + w / 2, 1), 4)]


def _catalog(texts: list) -> list[dict]:
    """目录页：按行分开（同一高度的文字算一行），一行里只有一格像图号的就是一条。"""
    rows: list[list] = []
    for t in sorted(texts, key=lambda t: (-t.y, t.x)):
        if rows and abs(rows[-1][0].y - t.y) <= max(rows[-1][0].h * 0.5, 1.0):
            rows[-1].append(t)
        else:
            rows.append([t])
    out = []
    for row in rows:
        cells = [c.text.strip() for c in sorted(row, key=lambda c: c.x)]
        nos = [c for c in cells if looks_like_no(c)]
        if len(nos) != 1:
            continue
        rest = [c for c in cells[cells.index(nos[0]) + 1:] if c]
        title = next((c for c in rest if not REV_CELL.match(c) and not re.fullmatch(r"[\d.\-/ :]+", c) and len(c) > 1), None)
        rev = next((c for c in rest if REV_CELL.match(c) and c != title), None)
        out.append({"drawing_no": _norm_no(nos[0]), "drawing_title": title, "revision": rev, "source": "cad",
                    "confidence": TEXT_CONF})
    return out


# ---------------- 写成识别结果 ----------------

def _field(value, conf) -> dict:
    return {"value": value or None, "raw_text": None, "source": "cad" if value else None, "confidence": conf if value else None}


def sheet_text(s: Sheet) -> str:
    """一张图的“文字层”：图签属性写成“属性名：值”，再跟图上的文字（从上到下、从左到右）。"""
    lines = [f"{t}：{v}" for t, v in s.attrs if v]
    lines += [t.text for t in sorted(s.texts, key=lambda t: (-t.y, t.x))]
    return "\n".join(lines)


def recognition(page_id: str, sha: str, path: str, index: int, count: int, s: Sheet, reader: str) -> dict:
    """一张图写成识别结果（spec/schema.json），执行器 dwg，来源 cad。"""
    f = s.fields
    no = _norm_no(f["drawing_no"]) if f.get("drawing_no") else None
    title = f.get("drawing_title")
    kind = "图纸"
    if re.search(r"目录|drawing\s*(?:list|register|index|schedule)", title or "", re.I):
        kind = "图纸目录"
    elif re.search(r"设计说明|总说明|施工说明|general\s*notes", title or "", re.I):
        kind = "设计说明"
    refs, seen = [], set()
    if kind != "图纸目录":
        for t in s.texts:
            for n in find_numbers(t.text, t.callout):
                if (no and _norm_no(n).upper() == no.upper()) or (n, t.text) in seen:
                    continue
                seen.add((n, t.text))
                refs.append({"raw_text": t.text[:200], "target_drawing_no": n,
                             "ref_type": "详图索引" if t.callout else "文字引用", "mark": t.mark if t.callout else None,
                             "bbox": _rel_box(s.box, t.x, t.y), "source": "cad", "confidence": TEXT_CONF})
    tb = {k: _field(f.get(k), s.conf.get(k)) for k in ("drawing_title", "discipline", "revision", "issue_date", "scale",
                                                        "project_name", "designer_org")}
    tb["drawing_no"] = _field(no, s.conf.get("drawing_no"))
    tb["revision_history"] = []
    tb["bbox"] = None
    issues = []
    if not no:
        issues.append({"type": "图签缺失", "description": "DWG 里没有认出图号，按文件名和文字推测。"})
    where = "模型空间" if s.space == MODEL else f"布局“{s.space}”"
    how = f"，图号按{s.how}认" if no and s.how else ""
    return {
        "schema_version": "0.1",
        "page_id": page_id,
        "source": {"file_path": path, "file_sha256": sha, "page_index": index, "page_count": count, "has_text_layer": True},
        "run": {"executor": EXECUTOR, "model": reader or "LibreDWG", "instruction_version": INSTRUCTION_VERSION,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "page_kind": kind,
        "title_block": tb,
        "references": refs,
        "catalog_entries": _catalog(s.texts) if kind == "图纸目录" else [],
        "content": {"summary": f"从 DWG 读出的图签和文字（{where}{how}，没有看图）。", "notes": [], "tables": [],
                    "key_values": [], "standards": [], "details": [],
                    "coverage": {k: _field(None, None) for k in ("zone", "axis_x", "axis_y")}, "xrefs": sorted(s.xrefs)},
        "issues": issues,
    }


def page_hash(s: Sheet) -> str:
    """内容指纹：图签和文字一样就算同一份（代替 PDF 的像素指纹）。"""
    body = "\x1e".join(f"{t}={v}" for t, v in s.attrs) + "\x1f" + sheet_text(s)
    return "cad:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:40]


def page_size(s: Sheet) -> tuple[float, float]:
    return (round(s.box[2] - s.box[0], 2), round(s.box[3] - s.box[1], 2)) if s.box else (0.0, 0.0)


# ---------------- 登记进项目 ----------------

def unread(con, again: bool = False) -> list:
    """还没读过、或按旧规则（INSTRUCTION_VERSION 不同）读的 DWG、DXF（again 时全部）：
    [(sha256, 项目里存的路径, 扩展名, 第一个出处, 第一次收到的批次)]。"""
    rows = con.execute(
        "SELECT f.sha256, f.stored_path, f.ext, MIN(o.path) AS path, MIN(o.batch_seq) AS batch FROM files f"
        " JOIN occurrences o ON o.sha256 = f.sha256 WHERE f.kind = 'cad' AND f.ext IN ('.dwg', '.dxf')"
        + ("" if again else " AND f.sha256 NOT IN (SELECT sha256 FROM cad_reads WHERE version = ?)")
        + " GROUP BY f.sha256 ORDER BY batch, path", () if again else (INSTRUCTION_VERSION,)).fetchall()
    return [tuple(r) for r in rows]


def save(con, sha: str, path: str, batch: int, d: Drawing | None, error: str | None, now: str) -> int:
    """一个文件读的结果写进项目：每张图一页（pages）和一份识别结果（recognitions）；读不出的记进收图问题。
    同一个文件以前读过的先删掉（重读）。返回读出几张图。"""
    from .pdfpages import page_id
    old = [r[0] for r in con.execute("SELECT page_id FROM pages WHERE file_sha256=?", (sha,))]
    con.executemany("DELETE FROM recognitions WHERE page_id=?", [(x,) for x in old])
    con.execute("DELETE FROM pages WHERE file_sha256=?", (sha,))
    con.execute("DELETE FROM extraction_issues WHERE kind='cad_unreadable' AND path IN"
                " (SELECT path FROM occurrences WHERE sha256=?)", (sha,))
    sheets = split_sheets(d) if d is not None else []
    if error:
        con.execute("INSERT INTO extraction_issues(batch_seq, path, kind, detail) VALUES (?,?,?,?)",
                    (batch, path, "cad_unreadable", error))
    for n, s in enumerate(sheets, 1):
        pid = page_id(sha, n)
        w, h = page_size(s)
        text = sheet_text(s)
        con.execute("INSERT INTO pages(page_id, file_sha256, page_index, width, height, has_text, render_hash, dhash, text)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (pid, sha, n, w, h, int(bool(text)), page_hash(s), "0" * 64, text))
        rec = recognition(pid, sha, path, n, len(sheets), s, d.reader)
        con.execute("INSERT INTO recognitions(page_id, executor, model, instruction_version, finished_at, imported_at, data)"
                    " VALUES (?,?,?,?,?,?,?)", (pid, EXECUTOR, rec["run"]["model"], INSTRUCTION_VERSION,
                                                rec["run"]["finished_at"], now, json.dumps(rec, ensure_ascii=False)))
    con.execute("UPDATE files SET page_count=?, has_text_layer=? WHERE sha256=?",
                (len(sheets) or None, int(bool(sheets)) if d is not None else None, sha))
    status = "failed" if error else "ok" if sheets else "empty"
    con.execute("INSERT OR REPLACE INTO cad_reads(sha256, status, reader, sheets, detail, read_at, version) VALUES (?,?,?,?,?,?,?)",
                (sha, status, (d.reader if d else "") or "", len(sheets), error, now, INSTRUCTION_VERSION))
    return len(sheets)
