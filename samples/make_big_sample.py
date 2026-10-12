"""大项目合成样例：一栋超高层（虚构）约 1000 张图，用来测图谱在图纸很多时的显示和速度。

每张图一页，只有图签和几行说明（引用详图、剖面），识别用模拟执行器就够。
用法：python samples/make_big_sample.py <输出目录> [楼层数]
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pymupdf

PROJECT = "超高层合成大项目"
FONT = "china-s"
W, H = 842, 595  # A4 横向，够放图签


def page(doc, no, title, notes):
    p = doc.new_page(width=W, height=H)
    y = 60
    for ln in notes:
        p.insert_text((60, y), ln, fontname=FONT, fontsize=9)
        y += 16
    tb = [f"工程名称 {PROJECT}", "设计单位 合成设计院", f"图名 {title}", f"图号 {no}", "版次 A", "日期 2026-05-01", "比例 1:100"]
    y = H * 0.7
    for ln in tb:
        p.insert_text((W * 0.62, y), ln, fontname=FONT, fontsize=8)
        y += 12


def sheets(floors: int):
    """(专业目录, 图号, 图名, 说明) 的清单。"""
    out = []
    fl = [f"B{i}" for i in range(3, 0, -1)] + [f"{i}F" for i in range(1, floors + 1)] + ["屋顶"]
    cn = lambda f: f"地下{'一二三'[int(f[1:]) - 1]}层" if f.startswith("B") else ("屋顶" if f == "屋顶" else f"{f[:-1]}层")  # noqa: E731
    n = 0
    out.append(("建筑", "建施-000", "建筑图纸目录", []))
    out.append(("建筑", "建施-001", "建筑设计总说明", ["墙身构造详见建施-301", "楼梯详见建施-401"]))
    for i, f in enumerate(fl, 1):
        out.append(("建筑", f"建施-{100 + i:03d}", f"{cn(f)}平面图", [f"墙身详见建施-{301 + i % 20:03d}", "核心筒详见建施-501",
                                                                   "1-1 剖面见建施-201"]))
    for i in range(1, 9):
        out.append(("建筑", f"建施-{200 + i:03d}", f"{i}-{i}剖面图", ["墙身详见建施-301"]))
    for i in range(1, 41):
        out.append(("建筑", f"建施-{300 + i:03d}", f"墙身大样{i}", []))
    for i in range(1, 11):
        out.append(("建筑", f"建施-{400 + i:03d}", f"楼梯详图{i}", []))
    for i in range(1, 11):
        out.append(("建筑", f"建施-{500 + i:03d}", f"核心筒详图{i}", []))
    out.append(("结构", "结施-000", "结构图纸目录", []))
    out.append(("结构", "结施-001", "结构设计总说明", ["节点构造详见结施-601"]))
    for i, f in enumerate(fl, 1):
        out.append(("结构", f"结施-{100 + i:03d}", f"{cn(f)}梁配筋图", ["梁柱节点详见结施-601", "未注明者详见结施-001"]))
        out.append(("结构", f"结施-{200 + i:03d}", f"{cn(f)}板配筋图", ["未注明者详见结施-001"]))
        out.append(("结构", f"结施-{300 + i:03d}", f"{cn(f)}墙柱平面图", ["墙柱详见结施-501"]))
    for i in range(1, 21):
        out.append(("结构", f"结施-{500 + i:03d}", f"墙柱详图{i}", []))
    for i in range(1, 11):
        out.append(("结构", f"结施-{600 + i:03d}", f"节点详图{i}", []))
    for prefix, disc, kinds in (("水施", "给排水", ["给排水平面图"]), ("暖施", "暖通", ["通风平面图", "空调水平面图"]),
                                ("电施", "电气", ["照明平面图", "动力平面图"])):
        out.append((disc, f"{prefix}-000", f"{disc}图纸目录", []))
        out.append((disc, f"{prefix}-001", f"{disc}设计说明", []))
        for k, kind in enumerate(kinds):
            for i, f in enumerate(fl, 1):
                out.append((disc, f"{prefix}-{100 * (k + 1) + i:03d}", f"{cn(f)}{kind}", [f"系统图见{prefix}-901"]))
        out.append((disc, f"{prefix}-901", f"{disc}系统图", []))
    return out


def build(out: Path, floors: int = 60) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    rows = sheets(floors)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for disc, no, title, notes in rows:
            doc = pymupdf.open()
            page(doc, no, title, notes)
            z.writestr(f"{PROJECT}/{disc}/{no} {title}.pdf", doc.tobytes())
    p = out / f"{PROJECT}.zip"
    p.write_bytes(buf.getvalue())
    print(f"{len(rows)} 张图 → {p}")
    return p


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "big-out"), int(sys.argv[2]) if len(sys.argv) > 2 else 60)
