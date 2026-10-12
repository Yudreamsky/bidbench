"""建模找图演练用的合成图纸：一栋 4 层钢筋混凝土框架办公楼（虚构），标准答案已知。

演练看的是“找图”：建模的人按任务（建二层结构梁、建一层墙体门窗、建楼梯、建屋面女儿墙……）能不能
很快找到要用的现行图纸，知道相关的剖面、大样、详图（包括没发来的），并按需跳过去打开。
所以这套图里画了详图索引符号（①/建施-07）、剖切符号、文字引用（详见建施-08），大样做成合订本，
还有一张被引用但没发的大样、一张变更单。TASKS 是每个任务该找到的图和该注意的事。

- 批次1 首次发图：建施-00~08、建施-10（建施-07、08、10 在一本详图合订本里），结施-00~07；
  建施-09 卫生间大样、结施-08 板配筋图目录列出但没发
- 批次2 结构修改：结施-03 二层梁配筋图升 B 版；结施-00 目录升 B 版，把结施-05 写成 B 版但没发
- 批次3 设计变更单-003：二层 KL3 截面改为 250×550；建施-07 ② 女儿墙高度改为 1.500 m

用法：python samples/make_model_sample.py <输出目录>
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pymupdf

PROJECT = "建模演练办公楼"
ORG = "演练建筑设计研究院"
FONT = "china-s"
W, H = 1190, 842  # A3 横向

X_AXES, Y_AXES = ["1", "2", "3", "4", "5"], ["A", "B", "C", "D"]
X_SPANS, Y_SPANS = [8400, 8400, 8400, 6000], [7200, 2700, 7200]
LEVELS = [  # 楼层、建筑标高、结构标高、层高（m）
    ("屋面", 16.200, 16.150, None), ("4F", 12.300, 12.250, 3.900), ("3F", 8.400, 8.350, 3.900),
    ("2F", 4.500, 4.450, 3.900), ("1F", 0.000, -0.050, 4.500),
]
KZ1_AT = ["B2", "B3", "B4", "C2", "C3", "C4"]
DOORS_WINDOWS = [("M1021", "1000×2100", "24", "木门"), ("M1521", "1500×2100", "4", "钢质门"),
                 ("C1518", "1500×1800", "40", "铝合金窗"), ("C2418", "2400×1800", "12", "铝合金窗")]
DETAILS_PDF = "建施-07~08、10 详图合订本.pdf"

# ---------------- 演练任务和标准答案 ----------------
# need：建这个模型必须看的图；related：图上引用到、按需要打开的剖面、大样、详图；
# missing：引用到但没发来的图；notices：改过这些图的变更单；cautions：要提醒建模的人注意的事。
TASKS = [
    {"id": "T1", "task": "建二层结构梁（含梁柱节点）",
     "start": "从二层梁配筋图开始",
     "need": ["结施-03", "结施-02", "结施-01"], "related": ["结施-07", "结施-06"], "missing": [],
     "notices": ["设计变更单-003"],
     "cautions": ["结施-03 用 B 版（批次#2 升版，KL2 300×600 → 300×650）",
                  "设计变更单-003 把二层 KL3 改为 250×550"]},
    {"id": "T2", "task": "建一层建筑墙体和门窗",
     "start": "从一层平面图开始",
     "need": ["建施-02", "建施-06", "建施-01"], "related": ["建施-07", "建施-05"], "missing": ["建施-09"],
     "notices": [],
     "cautions": ["外墙墙身、窗台看建施-07 的 ①、③（在详图合订本第 1 页）",
                  "1-1、2-2 剖面在建施-05", "卫生间大样建施-09 没发来"]},
    {"id": "T3", "task": "建楼梯",
     "start": "从一层平面图上“楼梯详见建施-08”开始",
     "need": ["建施-08", "结施-06"], "related": ["建施-02", "建施-05"], "missing": [],
     "notices": [],
     "cautions": ["建施-08 在详图合订本第 2 页", "梯梁、梯板配筋看结施-06"]},
    {"id": "T4", "task": "建屋面和女儿墙",
     "start": "从屋顶平面图开始",
     "need": ["建施-04", "结施-05"], "related": ["建施-07", "建施-10"], "missing": [],
     "notices": ["设计变更单-003"],
     "cautions": ["结施-05 目录写 B 版，手上只有 A 版，可能过时",
                  "女儿墙大样是建施-07 的 ②，变更单-003 把女儿墙高度改为 1.500 m",
                  "屋面节点建施-10 在详图合订本第 3 页"]},
    {"id": "T5", "task": "核对楼层标高（建筑、结构）",
     "start": "从剖面图开始",
     "need": ["建施-05", "结施-02"], "related": ["建施-01", "结施-01"], "missing": [],
     "notices": [],
     "cautions": ["建筑标高比结构标高高 50 mm（面层），不是冲突"]},
]


# ---------------- 画图 ----------------

def _t(page, x, y, s, size=9, rotate=0):
    page.insert_text((x, y), s, fontname=FONT, fontsize=size, rotate=rotate)


def _sheet(doc, no, title, rev, date, history, scale="1:100"):
    """新的一页：图框、图签。"""
    page = doc.new_page(width=W, height=H)
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(20, 20, W - 20, H - 20))
    sh.draw_rect(pymupdf.Rect(W * 0.66, H * 0.72, W - 24, H - 24))
    sh.finish(color=(0, 0, 0), width=0.8)
    sh.commit()
    tb = [f"工程名称 {PROJECT}", f"设计单位 {ORG}", f"图名 {title}", f"图号 {no}", f"版次 {rev}", f"日期 {date}",
          f"比例 {scale}"] + [f"修改记录 {r} {d} {t}" for r, d, t in history]
    y = H * 0.72 + 18
    for ln in tb:
        _t(page, W * 0.66 + 10, y, ln)
        y += 14
    return page


def _grid(page, box=(110, 120, 700, 560)):
    """轴网：轴线、轴号圈、两道尺寸。返回各交点坐标，如 {"B2": (x, y)}。A 轴在下，1 轴在左。"""
    x0, y0, x1, y1 = box
    s = min((x1 - x0) / sum(X_SPANS), (y1 - y0) / sum(Y_SPANS))
    xs = [x0]
    for sp in X_SPANS:
        xs.append(xs[-1] + sp * s)
    ys = [y0 + sum(Y_SPANS) * s]
    for sp in Y_SPANS:
        ys.append(ys[-1] - sp * s)
    sh = page.new_shape()
    for x in xs:
        sh.draw_line((x, ys[-1] - 46), (x, ys[0] + 14))
        sh.draw_circle((x, ys[-1] - 58), 11)
    for y in ys:
        sh.draw_line((x0 - 46, y), (xs[-1] + 14, y))
        sh.draw_circle((x0 - 58, y), 11)
    sh.finish(color=(0, 0, 0), width=0.6)
    sh.commit()
    for name, x in zip(X_AXES, xs):
        _t(page, x - 3, ys[-1] - 54, name, 10)
    for name, y in zip(Y_AXES, ys):
        _t(page, x0 - 62, y + 4, name, 10)
    for i, sp in enumerate(X_SPANS):
        _t(page, (xs[i] + xs[i + 1]) / 2 - 12, ys[-1] - 76, str(sp))
    _t(page, (xs[0] + xs[-1]) / 2 - 14, ys[-1] - 92, str(sum(X_SPANS)))
    for j, sp in enumerate(Y_SPANS):
        _t(page, x0 - 84, (ys[j] + ys[j + 1]) / 2 + 12, str(sp), rotate=90)
    _t(page, x0 - 100, (ys[0] + ys[-1]) / 2 + 14, str(sum(Y_SPANS)), rotate=90)
    return {f"{ya}{xa}": (x, y) for xa, x in zip(X_AXES, xs) for ya, y in zip(Y_AXES, ys)}


def _index(page, x, y, num, sheet):
    """详图索引符号：圆圈中间一道横线，上面是详图编号，下面是详图所在的图号。"""
    sh = page.new_shape()
    sh.draw_circle((x, y), 16)
    sh.draw_line((x - 16, y), (x + 16, y))
    sh.draw_line((x - 16, y), (x - 40, y + 18))  # 引出线
    sh.finish(color=(0, 0, 0), width=0.7)
    sh.commit()
    _t(page, x - 3, y - 4, str(num), 9)
    _t(page, x - 14, y + 11, sheet, 6)


def _section(page, x, y1, y2, name, sheet):
    """剖切符号：两端的短粗线和编号，旁边注明剖面画在哪张图上。"""
    sh = page.new_shape()
    for y in (y1, y2):
        sh.draw_line((x, y), (x, y + (14 if y == y1 else -14)))
        sh.draw_line((x, y), (x + 12, y))
    sh.finish(color=(0, 0, 0), width=2.2)
    sh.commit()
    _t(page, x + 14, y1 + 4, name, 10)
    _t(page, x + 14, y2 + 4, name, 10)
    _t(page, x + 30, y1 + 4, f"{name}-{name} 剖面见{sheet}", 7)


def _detail_title(page, x, y, num, title, scale):
    sh = page.new_shape()
    sh.draw_circle((x, y), 14)
    sh.finish(color=(0, 0, 0), width=1.2)
    sh.commit()
    _t(page, x - 4, y + 4, str(num), 11)
    _t(page, x + 22, y + 5, f"{title} {scale}", 10)


def _table(page, x, y, headers, rows, widths, title=None):
    if title:
        _t(page, x, y - 6, title, 10)
    sh = page.new_shape()
    hgt = 18
    for r in range(len(rows) + 2):
        sh.draw_line((x, y + r * hgt), (x + sum(widths), y + r * hgt))
    cx = x
    for w in [0, *widths]:
        cx += w
        sh.draw_line((cx, y), (cx, y + (len(rows) + 1) * hgt))
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    for r, row in enumerate([headers, *rows]):
        cx = x
        for w, cell in zip(widths, row):
            _t(page, cx + 4, y + r * hgt + 13, str(cell))
            cx += w
    return y + (len(rows) + 1) * hgt


def _notes(page, x, y, lines):
    for ln in lines:
        _t(page, x, y, ln)
        y += 16
    return y


def _levels_table(page, x, y):
    rows = [(f, f"{s:.3f}", f"{h:.3f}" if h else "—") for f, _a, s, h in LEVELS]
    return _table(page, x, y, ["层号", "结构标高(m)", "层高(m)"], rows, [60, 90, 70], title="结构层楼面标高")


def _pdf(*builders) -> bytes:
    doc = pymupdf.open()
    for b in builders:
        b(doc)
    return doc.tobytes()


def build(out: Path) -> list[tuple[Path, str]]:
    out.mkdir(parents=True, exist_ok=True)
    ha = [("A", "2026-03-01", "首次出图")]
    hb = ha + [("B", "2026-03-20", "按审图意见修改")]
    d1 = "2026-03-01"

    cat_jz = [("建施-00", "建筑图纸目录"), ("建施-01", "建筑设计总说明"), ("建施-02", "一层平面图"),
              ("建施-03", "二~四层平面图"), ("建施-04", "屋顶平面图"), ("建施-05", "剖面图"), ("建施-06", "门窗表"),
              ("建施-07", "墙身大样"), ("建施-08", "楼梯详图"), ("建施-09", "卫生间大样"), ("建施-10", "屋面节点详图")]
    cat_jg = [("结施-00", "结构图纸目录"), ("结施-01", "结构设计总说明"), ("结施-02", "柱平面布置图"),
              ("结施-03", "二层梁配筋图"), ("结施-04", "三~四层梁配筋图"), ("结施-05", "屋面梁配筋图"),
              ("结施-06", "楼梯结构详图"), ("结施-07", "梁柱节点详图"), ("结施-08", "板配筋图")]

    def catalog(no, title, rev, h, rows):
        def draw(doc):
            p = _sheet(doc, no, title, rev, h[-1][1], h, "—")
            _table(p, 80, 100, ["图号", "图名", "版次"], rows, [90, 220, 50], title="图纸目录")
        return draw

    def jz01(doc):
        p = _sheet(doc, "建施-01", "建筑设计总说明", "A", d1, ha, "—")
        _notes(p, 80, 100, ["1 本工程为多层办公楼，地上 4 层，无地下室。",
                            "2 室内外高差 0.450 m；楼面面层厚 50 mm，结构标高 = 建筑标高 - 0.050。",
                            "3 外墙为 200 厚加气混凝土砌块，墙身构造详见建施-07。",
                            "4 门窗尺寸、数量详见建施-06。",
                            "5 楼梯详见建施-08；卫生间详见建施-09。",
                            "6 屋面防水等级Ⅱ级，屋面节点详见建施-10。"])

    def plan(no, title, notes, extra):
        def draw(doc):
            p = _sheet(doc, no, title, "A", d1, ha)
            pts = _grid(p)
            extra(p, pts)
            _notes(p, 780, 100, notes)
        return draw

    def plan1_extra(p, pts):
        for name, k in (("A1", "M1521"), ("A3", "M1021"), ("D2", "C2418"), ("D4", "C1518")):
            x, y = pts[name]
            _t(p, x + 30, y + (16 if name[0] == "A" else -8), k, 8)
        _index(p, pts["A2"][0] + 60, pts["A2"][1] + 46, 1, "建施-07")   # 外墙墙身
        _index(p, pts["D3"][0] + 60, pts["D3"][1] - 40, 3, "建施-07")   # 窗台节点
        _section(p, pts["B2"][0] + 40, pts["D2"][1] - 26, pts["A2"][1] + 26, "1", "建施-05")
        _section(p, pts["B4"][0] + 40, pts["D4"][1] - 26, pts["A4"][1] + 26, "2", "建施-05")
        _t(p, pts["C4"][0] + 20, pts["C4"][1] + 30, "楼梯详见建施-08", 8)
        _t(p, pts["C2"][0] + 20, pts["C2"][1] + 30, "卫生间详见建施-09", 8)

    def plan2_extra(p, pts):
        _index(p, pts["A2"][0] + 60, pts["A2"][1] + 46, 1, "建施-07")
        _t(p, pts["C4"][0] + 20, pts["C4"][1] + 30, "楼梯详见建施-08", 8)
        _t(p, pts["C2"][0] + 20, pts["C2"][1] + 30, "卫生间详见建施-09", 8)

    def roof_extra(p, pts):
        _index(p, pts["D3"][0] + 60, pts["D3"][1] - 40, 2, "建施-07")   # 女儿墙
        _t(p, pts["B2"][0] + 20, pts["B2"][1] + 30, "屋面排水、出屋面节点详见建施-10", 8)

    def jz05(doc):
        p = _sheet(doc, "建施-05", "剖面图", "A", d1, ha)
        for i, (left, name) in enumerate(((70, "1"), (420, "2"))):
            sh = p.new_shape()
            base = 560
            k = 420 / (17.400 + 0.450)
            y_of = lambda h: base - (h + 0.450) * k  # noqa: E731
            sh.draw_line((left, base), (left + 300, base))
            sh.draw_rect(pymupdf.Rect(left + 40, y_of(17.400), left + 260, y_of(0.0)))
            for _f, a, _s, _h in LEVELS:
                sh.draw_line((left + 40, y_of(a)), (left + 290, y_of(a)))
            sh.finish(color=(0, 0, 0), width=0.6)
            sh.commit()
            _t(p, left + 100, base + 30, f"{name}-{name} 剖面图 1:100", 10)
            for f, a, _s, _h in LEVELS:
                _t(p, left + 262, y_of(a) - 2, ("±0.000" if a == 0 else f"{a:.3f}") + f" {f}", 7)
            _t(p, left + 200, y_of(17.400) - 2, "17.400", 7)
        _index(p, 700, 140, 1, "建施-07")
        _notes(p, 780, 100, ["剖面中的标高为建筑完成面标高。", "墙身构造详见建施-07。"])

    def jz06(doc):
        p = _sheet(doc, "建施-06", "门窗表", "A", d1, ha, "—")
        _table(p, 80, 100, ["编号", "洞口尺寸(宽×高)", "樘数", "类型"], DOORS_WINDOWS, [70, 130, 50, 90], title="门窗表")

    def jz07(doc):
        p = _sheet(doc, "建施-07", "墙身大样", "A", d1, ha, "1:20")
        for x, num, title, sc in ((80, 1, "外墙墙身大样", "1:20"), (380, 2, "女儿墙大样", "1:20"), (680, 3, "窗台节点", "1:10")):
            sh = p.new_shape()
            sh.draw_rect(pymupdf.Rect(x + 40, 120, x + 70, 520))
            sh.draw_line((x + 20, 300), (x + 120, 300))
            sh.finish(color=(0, 0, 0), width=0.8)
            sh.commit()
            _detail_title(p, x + 20, 560, num, title, sc)
        _t(p, 400, 140, "女儿墙高 1.200 m", 8)
        _t(p, 100, 140, "200 厚加气混凝土砌块", 8)

    def jz08(doc):
        p = _sheet(doc, "建施-08", "楼梯详图", "A", d1, ha, "1:50")
        _detail_title(p, 100, 560, 1, "楼梯平面详图", "1:50")
        _detail_title(p, 480, 560, 2, "楼梯剖面详图", "1:50")
        _notes(p, 780, 100, ["梯段宽 1.500 m，踏步 150×280。", "梯梁、梯板配筋详见结施-06。"])

    def jz10(doc):
        p = _sheet(doc, "建施-10", "屋面节点详图", "A", d1, ha, "1:10")
        _detail_title(p, 100, 560, 1, "落水口节点", "1:10")
        _detail_title(p, 480, 560, 2, "出屋面管道节点", "1:10")
        _notes(p, 780, 100, ["屋面构造见建施-01。"])

    def jg01(doc):
        p = _sheet(doc, "结施-01", "结构设计总说明", "A", d1, ha, "—")
        y = _notes(p, 80, 100, ["1 结构形式：钢筋混凝土框架结构。", "2 抗震设防烈度 7 度，框架抗震等级三级。",
                                "3 楼板厚度：未注明者为 120 mm，板配筋详见结施-08。",
                                "4 结构标高 = 建筑标高 - 0.050（楼面面层 50 mm）。",
                                "5 梁柱节点构造详见结施-07。"])
        _table(p, 80, y + 24, ["部位", "标高范围", "强度等级"],
               [("柱", "基础顶~8.350", "C35"), ("柱", "8.350~16.150", "C30"), ("梁、板", "全楼", "C30")],
               [70, 130, 70], title="混凝土强度等级")

    def jg02(doc):
        p = _sheet(doc, "结施-02", "柱平面布置图", "A", d1, ha)
        pts = _grid(p)
        sh = p.new_shape()
        for name, (x, y) in pts.items():
            s = 8 if name in KZ1_AT else 6
            sh.draw_rect(pymupdf.Rect(x - s, y - s, x + s, y + s))
        sh.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.5)
        sh.commit()
        for name, (x, y) in pts.items():
            _t(p, x + 10, y - 10, "KZ1" if name in KZ1_AT else "KZ2", 8)
        y = _table(p, 780, 100, ["柱号", "标高范围", "截面 b×h"],
                   [("KZ1", "基础顶~8.350", "600×600"), ("KZ1", "8.350~16.150", "500×500"),
                    ("KZ2", "基础顶~16.150", "500×500")], [50, 120, 80], title="柱表")
        _levels_table(p, 780, y + 40)

    def beams(no, title, rev, h, note, kl, prefix="KL", node_ref=True):
        def draw(doc):
            p = _sheet(doc, no, title, rev, h[-1][1], h)
            pts = _grid(p)
            k1, k2, k3 = kl
            _t(p, (pts["A2"][0] + pts["A3"][0]) / 2 - 30, pts["A2"][1] + 14, f"{prefix}1(4) {k1}", 8)
            _t(p, (pts["D2"][0] + pts["D3"][0]) / 2 - 30, pts["D2"][1] - 6, f"{prefix}1(4) {k1}", 8)
            _t(p, (pts["B2"][0] + pts["B3"][0]) / 2 - 30, pts["B2"][1] + 14, f"{prefix}2(4) {k2}", 8)
            _t(p, pts["B3"][0] + 6, (pts["B3"][1] + pts["C3"][1]) / 2 + 3, f"{prefix}3(1) {k3}", 8)
            if node_ref:
                _index(p, pts["B2"][0] + 40, pts["B2"][1] - 40, 2, "结施-07")
            lines = note + ["未注明梁顶标高同楼面结构标高。"] + (["梁柱节点详见结施-07。"] if node_ref else [])
            if no == "结施-03":
                lines.append("楼梯梁详见结施-06。")
            y = _notes(p, 780, 100, lines)
            _levels_table(p, 780, y + 40)
        return draw

    def jg06(doc):
        p = _sheet(doc, "结施-06", "楼梯结构详图", "A", d1, ha, "1:50")
        _detail_title(p, 100, 560, 1, "楼梯结构平面", "1:50")
        _detail_title(p, 480, 560, 2, "TB1 梯板配筋", "1:30")
        _notes(p, 780, 100, ["TL1 200×400", "TB1 板厚 130", "建筑做法见建施-08。"])

    def jg07(doc):
        p = _sheet(doc, "结施-07", "梁柱节点详图", "A", d1, ha, "1:20")
        _detail_title(p, 100, 560, 1, "边柱节点", "1:20")
        _detail_title(p, 480, 560, 2, "中柱节点", "1:20")

    def change(doc):
        p = _sheet(doc, "设计变更单-003", "设计变更通知单", "A", "2026-04-02", [("A", "2026-04-02", "变更")], "—")
        _notes(p, 80, 100, ["1 结施-03 二层梁配筋图：二层 KL3 截面由 250×500 改为 250×550。",
                            "2 建施-07 墙身大样 ② 女儿墙大样：女儿墙高度由 1.200 m 改为 1.500 m。", "3 其余不变。"])

    R = "演练办公楼施工图/"
    b1 = {
        f"{R}建筑/建施-00 建筑图纸目录.pdf": _pdf(catalog("建施-00", "建筑图纸目录", "A", ha, [(n, t, "A") for n, t in cat_jz])),
        f"{R}建筑/建施-01 建筑设计总说明.pdf": _pdf(jz01),
        f"{R}建筑/平面/建施-02 一层平面图.pdf": _pdf(plan("建施-02", "一层平面图", ["1F 层高 4.500 m", "门窗编号详见建施-06"], plan1_extra)),
        f"{R}建筑/平面/建施-03 二~四层平面图.pdf": _pdf(plan("建施-03", "二~四层平面图", ["适用楼层：2F~4F", "门窗编号详见建施-06"], plan2_extra)),
        f"{R}建筑/平面/建施-04 屋顶平面图.pdf": _pdf(plan("建施-04", "屋顶平面图", ["屋面找坡 2%"], roof_extra)),
        f"{R}建筑/建施-05 剖面图.pdf": _pdf(jz05),
        f"{R}建筑/建施-06 门窗表.pdf": _pdf(jz06),
        f"{R}建筑/详图/{DETAILS_PDF}": _pdf(jz07, jz08, jz10),
        f"{R}结构/结施-00 结构图纸目录.pdf": _pdf(catalog("结施-00", "结构图纸目录", "A", ha, [(n, t, "A") for n, t in cat_jg])),
        f"{R}结构/结施-01 结构设计总说明.pdf": _pdf(jg01),
        f"{R}结构/结施-02 柱平面布置图.pdf": _pdf(jg02),
        f"{R}结构/结施-03 二层梁配筋图.pdf": _pdf(beams("结施-03", "二层梁配筋图", "A", ha, ["本层结构标高 4.450"],
                                                    ("300×700", "300×600", "250×500"))),
        f"{R}结构/结施-04 三~四层梁配筋图.pdf": _pdf(beams("结施-04", "三~四层梁配筋图", "A", ha,
                                                       ["适用楼层：3F~4F（结构标高 8.350、12.250）"], ("300×700", "300×600", "250×500"))),
        f"{R}结构/结施-05 屋面梁配筋图.pdf": _pdf(beams("结施-05", "屋面梁配筋图", "A", ha, ["本层结构标高 16.150"],
                                                    ("300×600", "300×600", "250×500"), prefix="WKL", node_ref=False)),
        f"{R}结构/结施-06 楼梯结构详图.pdf": _pdf(jg06),
        f"{R}结构/结施-07 梁柱节点详图.pdf": _pdf(jg07),
    }
    revs_b = {"结施-00": "B", "结施-03": "B", "结施-05": "B"}  # 结施-05 写成 B，但没发
    b2 = {"结构修改0320/结施-00 结构图纸目录.pdf": _pdf(catalog("结施-00", "结构图纸目录", "B", hb,
                                                          [(n, t, revs_b.get(n, "A")) for n, t in cat_jg])),
          "结构修改0320/结施-03 二层梁配筋图.pdf": _pdf(beams("结施-03", "二层梁配筋图", "B", hb, ["本层结构标高 4.450"],
                                                       ("300×700", "300×650", "250×500")))}
    b3 = {"0402变更/设计变更单-003.pdf": _pdf(change)}

    result = []
    for name, entries, date in (("演练办公楼施工图.zip", b1, d1), ("结构修改0320.zip", b2, "2026-03-20"),
                                ("0402变更.zip", b3, "2026-04-02")):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, data in entries.items():
                z.writestr(n, data)
        (out / name).write_bytes(buf.getvalue())
        result.append((out / name, date))
    return result


if __name__ == "__main__":
    for p, date in build(Path(sys.argv[1] if len(sys.argv) > 1 else "samples/model-out")):
        print(f"{date}  {p}")
