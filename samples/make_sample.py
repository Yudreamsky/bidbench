"""生成一套合成的投标图纸收图批次（4 个压缩包 + 1 个文件夹），用来演示和测试整条流水线。

覆盖的情况：
- 批次1 首次发图：建施-09（目录列出）、结施-03（目录列出）缺失；建施-03 引用不存在的建施-12；立面剖面合订本；
  建筑平面图缺 3F（标准层只适用 4F~20F）和两层地下室
- 批次2 整套重发：多数图原样重发（文件去重）；若干图升 B 版；建施-05 消失（作废还是漏发）；
  结施-01 B 版把地上层数写成 21 层（事实冲突）；新增电施-01；夹带一个 docx；
  建施-00 B 版目录把建施-06 写成 B 版，合订本里却还是 A 版（目录版次比收到的新）；
  结构目录原样重发，仍写结施-01、04、05 为 A 版（目录版次比收到的旧）
- 批次3 部分修改：结施-04 升 C 版（KL3 截面改变）；内层压缩包里补来结施-03；一个加密压缩包；
  风险文件（内容都是无害的占位）：结构文件夹里的 acad.lsp、双扩展名的“查看说明.pdf.exe”、带宏的 xlsm、
  打开即运行脚本的结施-04 PDF
- 批次4 补充：电施-01 同为 B 版但内容不同（改图未升版）；设计变更单；压缩包文件名为 GBK 编码
- 批次5 文件夹（build_folder）：补发建施-09、建施-12；文件夹里直接放的“电气.zip”（外层包，不算内层）
  里面再套一个压缩包（内层）；夹着系统缩略图缓存 Thumbs.db 和 Office 临时锁文件 ~$*.docx（不该收）

另有单独的几批，不在演示流水线里：build_towers 两栋塔楼各一套编号相同的图（测试“塔楼 + 图号”的图纸身份）；
build_numbering 英文图号写成“项目-单位-楼栋-专业-类型-楼层-流水号”、有的后面再跟版次的一批（测试图号格式）；
build_cad 一批 CAD 图（DXF，测试读 DWG、DXF 的图签和文字，docs/图纸字段.md 9.7）。

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
    cat_jz_b = [(a, b, "B" if a in ("建施-00", "建施-01", "建施-02", "建施-04", "建施-06") else c)
                for a, b, c in cat_jz_a if a != "建施-05"]  # 建施-06 只有 A 版：目录写的版次比收到的新
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


FOLDER_NAME = "1020补充"


def build_folder(out: Path) -> tuple[Path, str]:
    """第 5 批：发图方直接给了一个文件夹（共享盘、网盘下载下来的那种），不是压缩包。"""
    root = out / FOLDER_NAME
    h = [("A", "2026-10-20", "补发")]
    files = {
        "建筑/建施-09 楼梯大样.pdf": sheet_pdf("建施-09", "楼梯大样", "A", "2026-10-20", ["梯段宽 1.30 m", "栏杆做法详见建施-08"], h, "1:50"),
        "建筑/建施-12 幕墙节点详图.pdf": sheet_pdf("建施-12", "幕墙节点详图", "A", "2026-10-20", ["幕墙龙骨 120×60"], h, "1:10"),
        "电气.zip": make_zip({
            "电施-02 动力平面图.pdf": sheet_pdf("电施-02", "动力平面图", "A", "2026-10-20", ["动力回路 P1~P4"], h),
            "补充/应急照明.zip": make_zip({
                "电施-03 应急照明平面图.pdf": sheet_pdf("电施-03", "应急照明平面图", "A", "2026-10-20", ["应急照明回路 E1~E2"], h),
            }),
        }),
        "建筑/Thumbs.db": b"\0" * 32,
        "~$招标说明.docx": "（Office 打开文档时留下的临时锁文件）".encode("utf-8"),
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root, "2026-10-20"


TOWERS_NAME = "双塔首批"


def build_towers(out: Path) -> tuple[Path, str]:
    """两栋塔楼（T1、T2）的文件夹里各有一套编号相同的图，另有“公共”文件夹里的总平面、一份放在最外层看不出是哪栋的补图。

    用来检查：同图号不同楼不混成一张、不误报版本冲突；缺图按楼各查各的（T1 目录列了建施-03、T1 一层平面引用了
    建施-04，这两张只有 T2 有）；T1 的图引用公共的总平面能找到；看不出是哪栋的补图要人指定。"""
    root = out / TOWERS_NAME
    h = [("A", "2026-09-01", "出图")]
    cat = [("建施-01", "一层平面图", "A"), ("建施-02", "二层平面图", "A"), ("建施-03", "三层平面图", "A")]

    def pdf(no, title, body=(), variant=0, catalog=None):
        return sheet_pdf(no, title, "A", "2026-09-01", list(body), h, variant=variant, catalog=catalog)
    files = {
        "T1/建筑/建施-00 建筑图纸目录.pdf": pdf("建施-00", "建筑图纸目录", catalog=cat),
        "T1/建筑/建施-01 一层平面图.pdf": pdf("建施-01", "一层平面图", ["墙身详见建施-04"]),
        "T1/建筑/建施-02 二层平面图.pdf": pdf("建施-02", "二层平面图", ["总平面详见建施-90"]),
        "T1/结构/结施-01 一层结构平面图.pdf": pdf("结施-01", "一层结构平面图"),
        "T2/建筑/建施-00 建筑图纸目录.pdf": pdf("建施-00", "建筑图纸目录", variant=1, catalog=cat),
        "T2/建筑/建施-01 一层平面图.pdf": pdf("建施-01", "一层平面图", ["墙身详见建施-04"], variant=1),
        "T2/建筑/建施-02 二层平面图.pdf": pdf("建施-02", "二层平面图", variant=1),
        "T2/建筑/建施-03 三层平面图.pdf": pdf("建施-03", "三层平面图", variant=1),
        "T2/建筑/建施-04 墙身大样.pdf": pdf("建施-04", "墙身大样", variant=1),
        "T2/结构/结施-01 一层结构平面图.pdf": pdf("结施-01", "一层结构平面图", variant=1),
        "公共/建施-90 总平面图.pdf": pdf("建施-90", "总平面图"),
        "建施-02 二层平面图（补）.pdf": pdf("建施-02", "二层平面图", variant=2),
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root, "2026-09-01"


NUMBERING_NAME = "英文编号首批"


def build_numbering(out: Path) -> tuple[Path, str]:
    """英文图号写成“项目-单位-楼栋-专业-类型-楼层-流水号”（PRJ1-ORG-T1-AR-DWG-GF-1001），有的文件名后面再跟版次（-00）。

    用来检查图号格式（docs/图纸字段.md 9.5）：软件按文件名猜出各段（楼栋、专业、类型、楼层、可有可无的版次）；
    确认之前只用来分组显示；确认之后按楼栋段分楼、专业段认专业（SE 要人说是安防）、楼层段换算楼层，
    同一张图带不带“-00”的两份文件并成一张，前导零保留（0001 不变成 01）；分包单位另一种写法的图号套不上，单列出来。
    文件都放在不说明专业的 Drawings 文件夹里，专业只能从图号看出来。"""
    root = out / NUMBERING_NAME
    h = [("00", "2026-09-01", "ISSUED FOR TENDER")]

    def pdf(no, title, variant=0):
        return sheet_pdf(no, title, "00", "2026-09-01", [], h, variant=variant)
    files = {}
    levels = (("GF", "GROUND FLOOR"), ("01", "LEVEL 01"), ("LR", "LOWER ROOF"))
    disciplines = (("AR", "GA PLAN"), ("ST", "SLAB LAYOUT"), ("EL", "LIGHTING LAYOUT"), ("FP", "SPRINKLER LAYOUT"),
                   ("SE", "SECURITY LAYOUT"))
    for b in ("T1", "T2"):
        for k, (d, what) in enumerate(disciplines):
            for j, (lv, name) in enumerate(levels):
                no = f"PRJ1-ORG-{b}-{d}-DWG-{lv}-{1001 + 100 * k + j:04d}"
                rev = "-00" if (k + j) % 2 else ""  # 有的文件名在图号后面带版次
                files[f"Drawings/{no}{rev} - {name} {what}.pdf"] = pdf(no, f"{name} {what}")
    files.update({
        # 同一张图的另一份文件：文件名带版次后缀，内容略有不同（另一个出图包）
        "Drawings/IFC/PRJ1-ORG-T1-AR-DWG-GF-1001-00 - GROUND FLOOR GA PLAN.pdf":
            pdf("PRJ1-ORG-T1-AR-DWG-GF-1001", "GROUND FLOOR GA PLAN", 1),
        "Drawings/PRJ1-ORG-XX-AR-DWG-XX-0001 - DRAWING LIST.pdf": pdf("PRJ1-ORG-XX-AR-DWG-XX-0001", "DRAWING LIST"),
        "Drawings/PRJ1-ORG-T2-EL-DWG-01-1901 - SMALL POWER LAYOUT.pdf": pdf("PRJ1-ORG-T2-EL-DWG-01-1901", "SMALL POWER LAYOUT"),
        "Drawings/PRJ1-ORG-T1-AR-DET-XX-5001-00 - TYPICAL WALL DETAILS.pdf":
            pdf("PRJ1-ORG-T1-AR-DET-XX-5001", "TYPICAL WALL DETAILS"),
        "Drawings/PRJ1-ORG-T2-ST-SCH-XX-0002 - COLUMN SCHEDULE.pdf": pdf("PRJ1-ORG-T2-ST-SCH-XX-0002", "COLUMN SCHEDULE"),
        "Drawings/PRJ1-ORG-XX-EL-SPEC-XX-0003 - ELECTRICAL SPECIFICATION.pdf":
            pdf("PRJ1-ORG-XX-EL-SPEC-XX-0003", "ELECTRICAL SPECIFICATION"),
        "Drawings/SUB-ZZ-GEN-XX-1009-01 - GENERAL PILING LAYOUT.pdf": pdf("SUB-ZZ-GEN-XX-1009-01", "GENERAL PILING LAYOUT"),
    })
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root, "2026-09-01"


# ---------------- 分步识别 ----------------

STAGED_NAME = "分步识别"


def build_staged(out: Path) -> tuple[Path, str]:
    """一批几种页面混在一起的英文图（分步识别，docs/图纸字段.md 9.8），收图后每页该归哪一步：

    - 重点图：A-001 图纸目录（列了 A-101~A-103、A-301、A-501）、A-601 门表（Schedule）、
      投标答疑回复（Tender Query Response，没有图号）、5 页的照明规范书的头两页；
    - 建模主图：A-101 一层平面、A-301 剖面；
    - 按需深入：规范书第 3~5 页、A-501 楼梯详图、A-103 三层平面（同图号的 DXF 已读出内容）；
    - 合订本 RIBA 4 BINDER.pdf：第 1 页写着“图纸目录”，第 2 页只在文字里认出 A-102，文件名看不出种类——
      目录识别以前归按需深入，目录识别以后按目录上的图名（LEVEL 02 PLAN）归到建模主图。"""
    root = out / STAGED_NAME
    d = "2026-10-10"
    listed = [("A-101", "LEVEL 01 PLAN", "A"), ("A-102", "LEVEL 02 PLAN", "A"), ("A-103", "LEVEL 03 PLAN", "A"),
              ("A-301", "SECTION A-A", "A"), ("A-501", "STAIR DETAILS", "A")]
    spec = [{"no": "", "title": f"SPECIFICATION LIGHTING {i}", "rev": "A", "date": d,
             "body": [f"Section {i}: luminaires shall comply with the schedule"]} for i in range(1, 6)]
    files = {
        "A-001 图纸目录 DRAWING LIST.pdf": sheet_pdf("A-001", "图纸目录 DRAWING LIST", "A", d, [], catalog=listed),
        "A-601 DOOR SCHEDULE.pdf": sheet_pdf("A-601", "DOOR SCHEDULE", "A", d, ["D1 900x2100", "D2 1200x2100"]),
        "Tender Query Response 01.pdf": sheet_pdf("TQ-01", "TENDER QUERY RESPONSE", "A", d, ["Q1: door D1 width revised"]),
        "Specification - Lighting.pdf": merged_pdf(spec),
        "A-101 LEVEL 01 PLAN.pdf": sheet_pdf("A-101", "LEVEL 01 PLAN", "A", d, []),
        "A-301 SECTION A-A.pdf": sheet_pdf("A-301", "SECTION A-A", "A", d, []),
        "A-501 STAIR DETAILS.pdf": sheet_pdf("A-501", "STAIR DETAILS", "A", d, []),
        "A-103 LEVEL 03 PLAN.pdf": sheet_pdf("A-103", "LEVEL 03 PLAN", "A", d, []),
        "A-103 LEVEL 03 PLAN.dxf": cad_dxf([{"no": "A-103", "title": "LEVEL 03 PLAN", "rev": "A", "date": d,
                                            "texts": [(50, 250, "LEVEL 03 PLAN")]}]),
        "RIBA 4 BINDER.pdf": merged_pdf([
            {"no": "A-000", "title": "图纸目录 BINDER CONTENTS", "rev": "A", "date": d, "body": []},
            {"no": "A-102", "title": "SHEET 2", "rev": "A", "date": d, "body": []}]),
    }
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root, d


# ---------------- CAD 图（DXF） ----------------

TB_W, TB_H = 420, 297  # 图框（A3），图签属性在右下角


def _dxf(pairs) -> list[str]:
    out = []
    for code, value in pairs:
        out += [str(code), str(value)]
    return out


def cad_dxf(sheets=(), layouts=(), xrefs=(), viewports=(), frames=()) -> bytes:
    """一个 DXF（2007 格式，UTF-8）。sheets：模型空间里一排图框，每个 {no, title, rev, date, texts: [(x, y, 字)],
    callouts: [(x, y, 详图号, 所在图号)], tags: 属性名}；layouts：[(布局名, 图签属性 {属性名: 值} 或 None, [(x, y, 字)])]，
    布局里没有图签块时，图签写成普通文字（Revit 导出的图常这样）；xrefs：[(参照名, 绑定了没有, 布局名或 None, x, y)]，
    附着的参照块记着参照文件的路径，绑定进来的参照里面的块改名成“参照名$0$原名”；
    viewports：[(布局名, 视口在布局上的中心 x, y, 宽, 高, 看到的中心 x, y（视图坐标）, 高[, 扭转角, 目标点 x, y])]；
    frames：[(布局名, [(x, y, 标签)])]，Revit 导出的图框：标签写在图框块的块定义里（没有属性），插在布局的 (0, 0)，
    值是布局上散放的文字（写在 layouts 里）。"""
    tags = ("图号", "图名", "版次", "日期", "比例")
    lines = _dxf([(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1021"), (0, "ENDSEC"),
                  (0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (8, 0), (2, "TK"), (70, 2), (10, 0), (20, 0), (30, 0)])
    for (x1, y1), (x2, y2) in [((0, 0), (TB_W, 0)), ((TB_W, 0), (TB_W, TB_H)), ((TB_W, TB_H), (0, TB_H)), ((0, TB_H), (0, 0))]:
        lines += _dxf([(0, "LINE"), (8, 0), (10, x1), (20, y1), (30, 0), (11, x2), (21, y2), (31, 0)])
    lines += _dxf([(0, "ENDBLK"), (8, 0), (0, "BLOCK"), (8, 0), (2, "SYM"), (70, 2), (10, 0), (20, 0), (30, 0),
                   (0, "ENDBLK"), (8, 0)])
    for k, (_space, labels) in enumerate(frames):
        lines += _dxf([(0, "BLOCK"), (8, 0), (2, f"TB-FRAME-{k}"), (70, 0), (10, 0), (20, 0), (30, 0)])
        for (x1, y1), (x2, y2) in [((0, 0), (TB_W, 0)), ((TB_W, 0), (TB_W, TB_H)), ((TB_W, TB_H), (0, TB_H)), ((0, TB_H), (0, 0))]:
            lines += _dxf([(0, "LINE"), (8, 0), (10, x1), (20, y1), (30, 0), (11, x2), (21, y2), (31, 0)])
        for x, y, label in labels:
            lines += _dxf([(0, "TEXT"), (8, 0), (10, x), (20, y), (30, 0), (40, 2.5), (1, label)])
        lines += _dxf([(0, "ENDBLK"), (8, 0)])
    for name, bound in dict.fromkeys((n, b) for n, b, *_ in xrefs):
        head = [(70, 0)] if bound else [(70, 4), (1, f"..\\参照\\{name}.dwg")]
        lines += _dxf([(0, "BLOCK"), (8, 0), (2, name), *head, (10, 0), (20, 0), (30, 0), (0, "ENDBLK"), (8, 0)])
        if bound:
            lines += _dxf([(0, "BLOCK"), (8, 0), (2, f"{name}$0$WALL"), (70, 0), (10, 0), (20, 0), (30, 0), (0, "ENDBLK"), (8, 0)])
    lines += _dxf([(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")])

    def text(x, y, t, h=3.5, space=None):
        return _dxf([(0, "TEXT"), (8, 0), *([(67, 1), (410, space)] if space else []), (10, x), (20, y), (30, 0), (40, h), (1, t)])

    def insert(block, x, y, attrs, space=None):
        out = _dxf([(0, "INSERT"), (8, 0), *([(67, 1), (410, space)] if space else []), (66, 1), (2, block),
                    (10, x), (20, y), (30, 0)])
        for k, (tag, val) in enumerate(attrs):
            out += _dxf([(0, "ATTRIB"), (8, 0), (10, x + 330), (20, y + 40 - 7 * k), (30, 0), (40, 3), (1, val), (2, tag), (70, 0)])
        return out + _dxf([(0, "SEQEND"), (8, 0)])

    for k, sh in enumerate(sheets):
        x0 = k * (TB_W + 80)
        t = sh.get("tags", tags)
        lines += insert("TK", x0, 0, list(zip(t, (sh["no"], sh["title"], sh["rev"], sh["date"], "1:100"))))
        for x, y, tx in sh.get("texts", ()):
            lines += text(x0 + x, y, tx)
        for x, y, n, sheet in sh.get("callouts", ()):
            lines += insert("SYM", x0 + x, y, [("NO", n), ("SHEET", sheet)])
    for name, attrs, texts in layouts:
        if attrs:
            lines += insert("TK", 0, 0, list(attrs.items()), space=name)
        for x, y, tx in texts:
            lines += text(x, y, tx, space=name)
    for k, (space, _labels) in enumerate(frames):
        lines += _dxf([(0, "INSERT"), (8, 0), (67, 1), (410, space), (2, f"TB-FRAME-{k}"), (10, 0), (20, 0), (30, 0)])
    for space in dict.fromkeys(v[0] for v in viewports):  # dwg2dxf 的写法：布局本身的视口在前，编号都写 1
        lines += _dxf([(0, "VIEWPORT"), (8, 0), (67, 1), (410, space), (10, TB_W / 2), (20, TB_H / 2), (30, 0), (40, TB_W),
                       (41, TB_H), (69, 1), (12, TB_W / 2), (22, TB_H / 2), (45, TB_H)])
        for _space, cx, cy, w, h, vx, vy, vh, *turn in (v for v in viewports if v[0] == space):
            twist, tx, ty = turn or (0, 0, 0)
            lines += _dxf([(0, "VIEWPORT"), (8, 0), (67, 1), (410, space), (10, cx), (20, cy), (30, 0), (40, w), (41, h),
                           (69, 1), (12, vx), (22, vy), (17, tx), (27, ty), (37, 0), (45, vh), (51, twist)])
    for name, _bound, space, x, y in xrefs:
        lines += _dxf([(0, "INSERT"), (8, 0), *([(67, 1), (410, space)] if space else []), (2, name), (10, x), (20, y), (30, 0)])
    lines += _dxf([(0, "ENDSEC"), (0, "EOF")])
    return ("\n".join(lines) + "\n").encode("utf-8")


CAD_NAME = "CAD补充"


def build_cad(out: Path) -> tuple[Path, str]:
    """一批 CAD 图（DXF；DWG 的读法相同，测试里用记下来的 LibreDWG 输出）：

    - 建施-03 二层平面图：PDF 和 DXF 都是 B 版（DWG 转出的 PDF）——配成一份，不报“改图未升版”；PDF 上没有的引用
      “详见建施-13”只在 DXF 里，也要算上；
    - 建施-04 三层平面图：PDF 是 A 版，DXF 是 B 版——DXF 是现行版；
    - 建施-13~14 楼梯详图：一个 DXF 的模型空间里排了两张图，各有图签；建施-14 的索引符号指向缺的建施-15；
    - PRJ1-A-701、A-702 核心筒放大图：一个 DXF 两个布局，图签属性名是自己编的（KD0105、JUDUL1/JUDUL2）；
      各有一个视口看模型空间的一块（翻模时只取这一块），A-702 的视口转了 90°；按 dwg2dxf 的写法，视口编号都是 1；
    - A-150 平面图：布局里的图签是普通文字（“Sheet No.”旁边写图号）；
    - PRJ-3036 标准层平面图：Revit 导出的图框，“Sheet No.”这些标签在图框块的块定义里（“Sheet”“No.”分两行写），
      值散放在布局上、写在标签下面那一格的中间，图号写成“-S-3036”（文件名用项目代号，图签是 S-3036）；
      修改记录的表头在布局上，A0 这一行却写死在图框块里、在表头上面（Revit 的表头在下、行往上长）；
    - 坏文件.dxf：二进制 DXF，读不出，按文件名暂定；
    - 外部参照：建施-03、建施-04 都附着了“标准层柱网”，A-701、A-702 都绑定了“核心筒-1”——共用参照的图连起来。"""
    root = out / CAD_NAME
    h = [("A", "2026-09-01", "出图"), ("B", "2026-10-01", "修改")]
    files = {
        "建筑/建施-03 二层平面图.pdf": sheet_pdf("建施-03", "二层平面图", "B", "2026-10-01", [], h),
        "建筑/建施-03 二层平面图.dxf": cad_dxf([{"no": "建施-03", "title": "二层平面图", "rev": "B", "date": "2026-10-01",
                                              "texts": [(50, 250, "二层平面图"), (60, 200, "屋面构架详见建施-13")]}],
                                            xrefs=[("标准层柱网", False, None, 100, 100)]),
        "建筑/建施-04 三层平面图.pdf": sheet_pdf("建施-04", "三层平面图", "A", "2026-09-01", [], h[:1]),
        "建筑/建施-04 三层平面图.dxf": cad_dxf([{"no": "建施-04", "title": "三层平面图", "rev": "B", "date": "2026-10-01",
                                              "texts": [(50, 250, "三层平面图")]}],
                                            xrefs=[("标准层柱网", False, None, 100, 100)]),
        "建筑/建施-13~14 楼梯详图.dxf": cad_dxf([
            {"no": "建施-13", "title": "楼梯详图一", "rev": "A", "date": "2026-10-01", "texts": [(50, 250, "1号楼梯")]},
            {"no": "建施-14", "title": "楼梯详图二", "rev": "A", "date": "2026-10-01", "texts": [(50, 250, "2号楼梯")],
             "callouts": [(100, 100, "3", "建施-15")]}]),
        "建筑/核心筒放大图.dxf": cad_dxf(layouts=[
            ("A-701", {"KD0105": "PRJ1-A-701", "JUDUL1": "ENLARGED CORE", "JUDUL2": "PLAN 1", "SKALA": "1:50"},
             [(60, 200, "CORE 1"), (60, 180, "REFER TO A-150 FOR SETTING OUT")]),
            ("A-702", {"KD0105": "PRJ1-A-702", "JUDUL1": "ENLARGED CORE", "JUDUL2": "PLAN 2", "SKALA": "1:50"},
             [(60, 200, "CORE 2")])],
            xrefs=[("核心筒-1", True, "A-701", 100, 100), ("核心筒-1", True, "A-702", 100, 100)],
            viewports=[("A-701", 200, 150, 300, 200, 15000, 10000, 20000),
                       ("A-702", 200, 150, 300, 200, 0, 0, 20000, 90, 45000, 10000)]),
        "建筑/A-150 平面图.dxf": cad_dxf(layouts=[
            ("Sheet", None, [(700, 40, "Sheet No."), (730, 40, "A-150"), (700, 60, "Drawing Title"),
                             (700, 52, "GENERAL ARRANGEMENT PLAN - LEVEL 12"), (100, 100, "OFFICE")])]),
        "结构/PRJ-3036 标准层平面图.dxf": cad_dxf(
            layouts=[("Layout1", None, [(702, 56, "-S-3036"), (703, 84, "TYPICAL OFFICE FLOOR PLAN"), (715, 31, "03/05/24"),
                                        (731, 47, "FOR INFORMATION"), (707, 20, "As"), (701, 17, "indicated"), (730, 20, "Rn"),
                                        (754, 20, "DA"), (702, 155, "REV"), (708, 155, "DATE"), (724, 155, "DESCRIPTION"),
                                        (100, 100, "OFFICE")])],
            frames=[("Layout1", [(700, 64, "Sheet"), (700, 61.5, "No."), (700, 90, "Sheet Name"), (700, 36, "Date"),
                                 (700, 33.3, "Drawn"), (731, 36, "Rev Date"), (762, 36, "Revision"), (700, 49, "Stage"),
                                 (700, 26, "Scale"), (712, 26, "Drawn By"), (737, 26, "Checked"),
                                 (700, 160.6, "A0"), (708, 160.6, "15/12/2025"), (725, 160.6, "FOR INFORMATION")])]),
        "建筑/坏文件.dxf": b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 64,
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root, "2026-10-01"


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/out")
    for p, date in [*build(target), build_folder(target)]:
        print(f"{date}  {p}")
