"""PDF 页面：拆页、文字层、页面指纹、渲染。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pymupdf

TEXT_LAYER_MIN_CHARS = 20


@dataclass
class PageInfo:
    page_index: int  # 从 1 开始
    width: float
    height: float
    has_text: bool
    render_hash: str
    dhash: str
    text: str


def page_id(file_sha: str, page_index: int) -> str:
    return f"{file_sha[:12]}-p{page_index:03d}"


def _render_hash(page: pymupdf.Page) -> str:
    """页面级指纹：低分辨率灰度渲染后的像素哈希，合订本中的一页与单独的 PDF 可对上。"""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), colorspace=pymupdf.csGRAY, alpha=False)
    h = hashlib.sha256()
    h.update(f"{pix.width}x{pix.height}".encode())
    h.update(pix.samples)
    return h.hexdigest()


def _dhash(page: pymupdf.Page) -> str:
    """内容级指纹：16x16 差值哈希，用于判断重新导出、轻微差异。"""
    r = page.rect
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(17 / r.width, 16 / r.height), colorspace=pymupdf.csGRAY, alpha=False
    )
    w, hgt, s = pix.width, pix.height, pix.samples
    bits = []
    for y in range(min(hgt, 16)):
        row = s[y * pix.stride : y * pix.stride + w]
        for x in range(min(w - 1, 16)):
            bits.append("1" if row[x] > row[x + 1] else "0")
    bits = "".join(bits).ljust(256, "0")
    return f"{int(bits, 2):064x}"


def dhash_distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def text_similarity(a: str, b: str) -> float:
    """字符三元组的 Jaccard 相似度。"""
    def grams(s: str) -> set[str]:
        s = "".join(s.split())
        return {s[i : i + 3] for i in range(max(len(s) - 2, 1))}

    ga, gb = grams(a), grams(b)
    if not ga and not gb:
        return 1.0
    return len(ga & gb) / max(len(ga | gb), 1)


def analyze_pdf(path: Path) -> tuple[list[PageInfo], list[tuple[str, bytes]]]:
    """返回 (逐页信息, 内嵌附件列表)。打不开时抛出异常，由调用方登记问题。"""
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            raise PermissionError("PDF 已加密，需要密码")
        pages: list[PageInfo] = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            pages.append(
                PageInfo(
                    page_index=i,
                    width=page.rect.width,
                    height=page.rect.height,
                    has_text=len("".join(text.split())) >= TEXT_LAYER_MIN_CHARS,
                    render_hash=_render_hash(page),
                    dhash=_dhash(page),
                    text=text,
                )
            )
        attachments: list[tuple[str, bytes]] = []
        for name in doc.embfile_names():
            info = doc.embfile_info(name)
            attachments.append((info.get("filename") or name, doc.embfile_get(name)))
        return pages, attachments
    finally:
        doc.close()


def text_lines(path: Path, page_index: int) -> list[dict]:
    """逐行文字及位置（bbox 归一化到 0~1，原点左上角）。"""
    doc = pymupdf.open(path)
    try:
        page = doc[page_index - 1]
        W, H = page.rect.width, page.rect.height
        out = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                t = "".join(span["text"] for span in line["spans"]).strip()
                if not t:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                out.append({
                    "text": t,
                    "bbox": [round(max(0, x0 / W), 4), round(max(0, y0 / H), 4),
                             round(min(1, x1 / W), 4), round(min(1, y1 / H), 4)],
                })
        return out
    finally:
        doc.close()


def render_page(path: Path, page_index: int, out: Path, dpi: int = 150, clip_frac: tuple | None = None) -> None:
    """渲染整页或局部（clip_frac 为相对页面的 [x0,y0,x1,y1]）。"""
    doc = pymupdf.open(path)
    try:
        page = doc[page_index - 1]
        clip = None
        if clip_frac:
            r = page.rect
            clip = pymupdf.Rect(r.width * clip_frac[0], r.height * clip_frac[1],
                                r.width * clip_frac[2], r.height * clip_frac[3])
        pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out)
    finally:
        doc.close()


def single_page_pdf(path: Path, page_index: int) -> bytes:
    """抽出单页另存为 PDF（合订本里的一张图也能单独打开、导出）。"""
    src = pymupdf.open(path)
    try:
        dst = pymupdf.open()
        dst.insert_pdf(src, from_page=page_index - 1, to_page=page_index - 1)
        data = dst.tobytes()
        dst.close()
        return data
    finally:
        src.close()


UNSAFE_ACTIONS = {"/JavaScript", "/Launch", "/RichMediaExecute", "/ImportData", "/SubmitForm", "/Rendition", "/GoToE"}


def clean_pdf(path: Path, page_index: int | None = None) -> bytes:
    """另存一份去掉脚本、启动程序、自动动作和附件的 PDF（图面内容不变）。page_index 为空时保留全部页面。"""
    src = pymupdf.open(path)
    try:
        dst = pymupdf.open()
        if page_index is None:
            dst.insert_pdf(src)
        else:
            dst.insert_pdf(src, from_page=page_index - 1, to_page=page_index - 1)
    finally:
        src.close()
    try:
        for x in range(1, dst.xref_length()):
            try:
                keys = dst.xref_get_keys(x)
            except Exception:  # noqa: BLE001 - 非字典对象
                continue
            for k in ("AA", "OpenAction", "JS", "JavaScript", "RichMediaContent", "EmbeddedFiles"):
                if k in keys:
                    dst.xref_set_key(x, k, "null")
            if "S" in keys and dst.xref_get_key(x, "S")[1] in UNSAFE_ACTIONS:
                dst.xref_set_key(x, "S", "null")
                for k in ("F", "Win", "Mac", "Unix"):
                    if k in keys:
                        dst.xref_set_key(x, k, "null")
            if "FS" in keys:  # 文件附件注释
                dst.xref_set_key(x, "FS", "null")
        for name in list(dst.embfile_names()):
            dst.embfile_del(name)
        return dst.tobytes(garbage=3, deflate=True)
    finally:
        dst.close()
