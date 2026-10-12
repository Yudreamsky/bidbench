"""生成程序图标 packaging/build/bidbench.ico（多尺寸，PNG 编码，Windows Vista 起都支持）。"""
import struct
from pathlib import Path

import pymupdf

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png(size: int) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=256, height=256)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(8, 8, 248, 248), radius=0.18)
    shape.finish(color=None, fill=(0.114, 0.373, 0.682))
    shape.draw_rect(pymupdf.Rect(52, 64, 204, 196))  # 图框
    shape.draw_line((52, 160), (204, 160))           # 图签线
    shape.draw_line((140, 160), (140, 196))
    shape.finish(color=(1, 1, 1), width=10)
    shape.commit()
    page.insert_text((92, 140), "标", fontname="china-s", fontsize=72, color=(1, 1, 1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(size / 256, size / 256), alpha=True,
                          clip=pymupdf.Rect(0, 0, 256, 256))
    return pix.tobytes("png")


def main() -> None:
    images = [png(s) for s in SIZES]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in zip(SIZES, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    out = Path(__file__).resolve().parent / "build" / "bidbench.ico"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(head + entries + blobs)
    (out.parent / "bidbench-256.png").write_bytes(images[-1])
    print(out)


if __name__ == "__main__":
    main()
