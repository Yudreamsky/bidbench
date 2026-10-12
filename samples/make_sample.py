"""生成一套合成的投标图纸收图批次（4 个压缩包），用来演示和测试整条流水线。

覆盖的情况：
- 批次1 首次发图：建施-09（目录列出）、结施-03（目录列出）缺失；建施-03 引用不存在的建施-12；立面剖面合订本；
  建筑平面图缺 3F（标准层只适用 4F~20F）和两层地下室
- 批次2 整套重发：多数图原样重发（文件去重）；若干图升 B 版；建施-05 消失（作废还是漏发）；
  结施-01 B 版把地上层数写成 21 层（事实冲突）；新增电施-01；夹带一个 docx
- 批次3 部分修改：结施-04 升 C 版（KL3 截面改变）；内层压缩包里补来结施-03；一个加密压缩包；
  风险文件（内容都是无害的占位）：结构文件夹里的 acad.lsp、双扩展名的“查看说明.pdf.exe”、带宏的 xlsm、
  打开即运行脚本的结施-04 PDF
- 批次4 补充：电施-01 同为 B 版但内容不同（改图未升版）；设计变更单；压缩包文件名为 GBK 编码

用法：python samples/make_sample.py <输出目录>
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pymupdf

PROJECT = "示例综合体项目"
ORG = "示例建筑设计研究院"
FONT = "china-s"
W, H = 1190, 842  # A3 横向


def sheet_pdf(no, title, rev, date, body, history=(), scale="1:100", variant=0, catalog=None) -> bytes:
    doc = pymupdf.open()
    _page(doc, no, title, rev, date, body, history, scale, variant, catalog)
    return doc.tobytes()


def _page(doc, no, title, rev, date, body, history=(), scale="1:100", variant=0, catalog=None):
    page = doc.new_page(width=W, height=H)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(20, 20, W - 20, H - 20))
    # 轴网和几个“构件”，让每张图的图面各不相同
    seed = sum(ord(c) for c in no + title) + variant * 97
    for i in range(7):
        x = 80 + i * 100
        shape.draw_line((x, 60), (x, 560))
    for j in range(5):
        y = 80 + j * 110
        shape.draw_line((60, y), (720, y))
    for k in range(4 + variant * 3):
        x = 90 + (seed * (k + 3)) % 560
        y = 90 + (seed * (k + 7)) % 420
        shape.draw_rect(pymupdf.Rect(x, y, x + 40 + (k * 13) % 60, y + 30 + (k * 7) % 40))
    # 图签框
    shape.draw_rect(pymupdf.Rect(W * 0.66, H * 0.72, W - 24, H - 24))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()

    y = 70
    lines = list(body)
    if catalog:
        lines = ["图号  图名  版次"] + [f"{a}  {b}  {c}" for a, b, c in catalog] + lines
    for ln in lines:
        page.insert_text((760, y), ln, fontname=FONT, fontsize=10)
        y += 18
    tb = [f"工程名称 {PROJECT}", f"设计单位 {ORG}", f"图名 {title}", f"图号 {no}", f"版次 {rev}",
          f"日期 {date}", f"比例 {scale}"] + [f"修改记录 {r} {d} {t}" for r, d, t in history]
    ty = H * 0.72 + 18
    for ln in tb:
        page.insert_text((W * 0.66 + 10, ty), ln, fontname=FONT, fontsize=9)
        ty += 14


def merged_pdf(sheets) -> bytes:
    doc = pymupdf.open()
    for s in sheets:
        _page(doc, **s)
    return doc.tobytes()


def make_zip(entries: dict[str, bytes], encrypt: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    data = buf.getvalue()
    if encrypt:  # 只置加密标志位，模拟一个需要密码的压缩包
        data = _patch_flags(data)
    return data


def _patch_flags(data: bytes) -> bytes:
    b = bytearray(data)
    i = 0
    while True:
        i = b.find(b"PK\x03\x04", i)
        if i < 0:
            break
        b[i + 6] |= 0x01
        i += 4
    i = 0
    while True:
        i = b.find(b"PK\x01\x02", i)
        if i < 0:
            break
        b[i + 8] |= 0x01
        i += 4
    return bytes(b)


def make_gbk_zip(entries: dict[str, bytes]) -> bytes:
    """Windows 资源管理器常见的打包方式：文件名用 GBK 编码，且不声明 UTF-8。"""
    placeholders = {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, data) in enumerate(entries.items()):
            gbk = name.encode("gbk")
            ph = f"~{i:03d}~".ljust(len(gbk), "q").encode()
            assert len(ph) == len(gbk)
            placeholders[ph] = gbk
            z.writestr(ph.decode(), data)
    data = buf.getvalue()
    for ph, gbk in placeholders.items():
        data = data.replace(ph, gbk)
    return data


def with_open_script(pdf: bytes) -> bytes:
    """给 PDF 加一个打开时运行的 JavaScript（只弹一句提示），模拟带脚本的图纸。"""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert\\('demo'\\))>>")
    return doc.tobytes()


# 无害的占位内容：只用来触发风险检查规则，不能运行
LISP_STUB = (";; 演示用占位文件，不做任何事。下面是 CAD 病毒常见写法的注释示例：\n"
             ";; (vl-file-copy \"acad.lsp\" \"acaddoc.lsp\")\n(princ)\n").encode("utf-8")
EXE_STUB = b"MZ" + b"\0" * 62 + "演示用占位文件，不是程序".encode("utf-8")


def xlsm_stub() -> bytes:
    return make_zip({"[Content_Types].xml": b"<?xml version='1.0'?><Types/>", "xl/workbook.xml": b"<workbook/>",
                     "xl/vbaProject.bin": b"demo placeholder"})


def docx_stub() -> bytes:
    return make_zip({"[Content_Types].xml": b"<?xml version='1.0'?><Types/>", "word/document.xml": "招标说明".encode()})


STD_FIRE = "《建筑设计防火规范》GB 50016-2014（2018年版）"


def build(out: Path) -> list[tuple[Path, str]]:
    out.mkdir(parents=True, exist_ok=True)
    R = "示例综合体投标图纸/"
    h_a = [("A", "2026-07-02", "首次出图")]
    h_b = h_a + [("B", "2026-08-02", "按甲方意见修改")]

    cat_jz_a = [("建施-00", "建筑图纸目录", "A"), ("建施-01", "建筑设计总说明", "A"), ("建施-02", "一层平面图", "A"),
                ("建施-03", "二层平面图", "A"), ("建施-04", "标准层平面图", "A"), ("建施-05", "屋顶平面图", "A"),
                ("建施-06", "南立面图", "A"), ("建施-07", "1-1剖面图", "A"), ("建施-08", "墙身大样", "A"),
                ("建施-09", "楼梯大样", "A")]
    cat_jz_b = [(a, b, "B" if a in ("建施-00", "建施-01", "建施-02", "建施-04") else c)
                for a, b, c in cat_jz_a if a != "建施-05"]
    cat_jg = [("结施-00", "结构图纸目录", "A"), ("结施-01", "结构设计总说明", "A"), ("结施-02", "基础平面布置图", "A"),
              ("结施-03", "剪力墙配筋图", "A"), ("结施-04", "三层梁配筋图", "A"), ("结施-05", "标准层梁配筋图", "A")]

    jz00a = sheet_pdf("建施-00", "建筑图纸目录", "A", "2026-07-02", [], h_a, "—", catalog=cat_jz_a)
    jz00b = sheet_pdf("建施-00", "建筑图纸目录", "B", "2026-08-02", [], h_b, "—", catalog=cat_jz_b)
    jz01 = lambda rev, h: sheet_pdf("建施-01", "建筑设计总说明", rev, h[-1][1], [  # noqa: E731
        "1 本工程地上 20 层，地下 2 层", "2 结构类型：框架剪力墙", "3 建筑高度 74.40 m",
        f"4 设计依据：{STD_FIRE}", "5 外墙做法详见建施-08", f"6 总建筑面积 {'45200' if rev == 'A' else '45860'} m2"], h, "—")
    jz02 = lambda rev, h, v=0: sheet_pdf("建施-02", "一层平面图", rev, h[-1][1], [  # noqa: E731
        "1F 层高 5.10 m", "楼梯详见建施-09", "外墙索引 ③/建施-08", "柱网对应结施-02"], h, variant=v)
    jz03 = sheet_pdf("建施-03", "二层平面图", "A", "2026-07-02", ["2F 层高 4.50 m", "幕墙节点详见建施-12", "楼面梁对应结施-04"], h_a)
    jz04 = lambda rev, h, v=0: sheet_pdf("建施-04", "标准层平面图", rev, h[-1][1], [  # noqa: E731
        "1 适用楼层：4F~20F", "3F~20F 层高 3.60 m", "外墙索引 ⑤/建施-08", "梁布置对应结施-05"], h, variant=v)
    jz05 = sheet_pdf("建施-05", "屋顶平面图", "A", "2026-07-02", ["屋面排水坡度 2%", "女儿墙详见建施-08"], h_a)
    jz0607 = merged_pdf([
        dict(no="建施-06", title="南立面图", rev="A", date="2026-07-02", body=["屋面标高 74.400", "屋顶设机房"], history=h_a),
        dict(no="建施-07", title="1-1剖面图", rev="A", date="2026-07-02",
             body=["地上 20 层", "建筑高度 74.40 m", "1F 层高 5.10 m", "2F 层高 4.50 m"], history=h_a),
    ])
    jz08 = sheet_pdf("建施-08", "墙身大样", "A", "2026-07-02", ["女儿墙高 1.20 m", "做法详见 12J201 图集"], h_a, "1:20")
    jg00 = sheet_pdf("结施-00", "结构图纸目录", "A", "2026-07-02", [], h_a, "—", catalog=cat_jg)
    jg01 = lambda rev, h, floors: sheet_pdf("结施-01", "结构设计总说明", rev, h[-1][1], [  # noqa: E731
        "1 结构形式：框架-剪力墙结构", "2 抗震设防烈度 7 度", f"3 地上 {floors} 层" + ("（含屋顶机房层）" if floors == 21 else ""),
        "4 混凝土强度等级 C30", "5 设计依据：《建筑抗震设计规范》GB 50011-2010（2016年版）",
        "6 设计依据：《混凝土结构设计规范》GB 50010-2010（2015年版）"], h, "—")
    jg02 = sheet_pdf("结施-02", "基础平面布置图", "A", "2026-07-02", ["未注明者详见结施-01"], h_a)
    jg03 = sheet_pdf("结施-03", "剪力墙配筋图", "A", "2026-09-15", ["Q1 墙厚 300", "未注明者详见结施-01"],
                     [("A", "2026-09-15", "补发")])
    jg04 = lambda rev, h, sec: sheet_pdf("结施-04", "三层梁配筋图", rev, h[-1][1], [  # noqa: E731
        f"KL3(2) {sec}", "未注明者详见结施-01"], h, variant={"A": 0, "B": 1, "C": 2}[rev])
    jg05 = lambda rev, h: sheet_pdf("结施-05", "标准层梁配筋图", rev, h[-1][1], ["1 适用楼层：4F~20F", "KL1(3) 300×650", "未注明者详见结施-01"], h,  # noqa: E731
                                    variant=0 if rev == "A" else 1)
    ws01 = sheet_pdf("水施-01", "给排水设计说明", "A", "2026-07-02",
                     [f"1 设计依据：{STD_FIRE}", "2 设计依据：《建筑给水排水设计标准》GB 50015-2019"], h_a, "—")
    ds01 = lambda v, note: sheet_pdf("电施-01", "照明平面图", "B", "2026-08-02" if v == 0 else "2026-10-08",  # noqa: E731
                                     [note], h_b, variant=v)
    change = sheet_pdf("设计变更单-012", "设计变更通知单", "A", "2026-10-06",
                       ["1 修改建施-08 女儿墙节点：女儿墙高改为 1.50 m", "2 其余不变"], [("A", "2026-10-06", "变更")], "—")

    b1 = {
        R + "建筑/建施-00 图纸目录.pdf": jz00a,
        R + "建筑/建施-01 建筑设计总说明.pdf": jz01("A", h_a),
        R + "建筑/平面图/建施-02 一层平面图.pdf": jz02("A", h_a),
        R + "建筑/平面图/建施-03 二层平面图.pdf": jz03,
        R + "建筑/平面图/建施-04 标准层平面图.pdf": jz04("A", h_a),
        R + "建筑/平面图/建施-05 屋顶平面图.pdf": jz05,
        R + "建筑/立面剖面合订.pdf": jz0607,
        R + "建筑/大样/建施-08 墙身大样.pdf": jz08,
        R + "结构/结施-00 结构图纸目录.pdf": jg00,
        R + "结构/结施-01 结构设计总说明.pdf": jg01("A", h_a, 20),
        R + "结构/结施-02 基础平面布置图.pdf": jg02,
        R + "结构/结施-04 三层梁配筋图.pdf": jg04("A", h_a, "300×600"),
        R + "结构/结施-05 标准层梁配筋图.pdf": jg05("A", h_a),
        R + "给排水/水施-01 给排水设计说明.pdf": ws01,
    }
    b2 = {k.replace("示例综合体投标图纸/", "示例综合体投标图纸0802/"): v for k, v in b1.items()
          if "建施-05" not in k}
    R2 = "示例综合体投标图纸0802/"
    b2.update({
        R2 + "建筑/建施-00 图纸目录.pdf": jz00b,
        R2 + "建筑/建施-01 建筑设计总说明.pdf": jz01("B", h_b),
        R2 + "建筑/平面图/建施-02 一层平面图.pdf": jz02("B", h_b, 1),
        R2 + "建筑/平面图/建施-04 标准层平面图.pdf": jz04("B", h_b, 1),
        R2 + "结构/结施-01 结构设计总说明.pdf": jg01("B", h_b, 21),
        R2 + "结构/结施-04 三层梁配筋图.pdf": jg04("B", h_b, "300×600"),
        R2 + "结构/结施-05 标准层梁配筋图.pdf": jg05("B", h_b),
        R2 + "电气/电施-01 照明平面图.pdf": ds01(0, "照明回路 N1~N6"),
        R2 + "招标说明.docx": docx_stub(),
    })
    h_c = h_b + [("C", "2026-09-15", "调整 KL3 截面")]
    b3 = {
        "0915修改/结施-04 三层梁配筋图(修).pdf": with_open_script(jg04("C", h_c, "350×700")),
        "0915修改/结构/acad.lsp": LISP_STUB,
        "0915修改/查看说明.pdf.exe": EXE_STUB,
        "0915修改/工程量清单.xlsm": xlsm_stub(),
        "0915修改/结构/补充图纸.zip": make_zip({"结施-03 剪力墙配筋图.pdf": jg03}),
        "0915修改/给排水/修改.zip": make_zip({"水施-02 给排水平面图.pdf": b"%PDF-1.4 placeholder"}, encrypt=True),
    }
    b4 = {
        "1008补充/电气/电施-01 照明平面图.pdf": ds01(1, "照明回路 N1~N8，增加应急照明"),
        "1008补充/设计变更单-012.pdf": change,
    }
    plan = [
        ("示例综合体投标图纸.zip", make_zip(b1), "2026-07-02"),
        ("示例综合体投标图纸0802.zip", make_zip(b2), "2026-08-02"),
        ("0915修改.zip", make_zip(b3), "2026-09-15"),
        ("1008补充.zip", make_gbk_zip(b4), "2026-10-08"),
    ]
    out_list = []
    for name, data, date in plan:
        p = out / name
        p.write_bytes(data)
        out_list.append((p, date))
    return out_list


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/out")
    for p, date in build(target):
        print(f"{date}  {p}")
