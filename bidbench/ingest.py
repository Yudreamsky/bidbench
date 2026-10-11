"""收图：登记批次、递归解包、按内容去重入库、登记解包问题。

原则：
- 原始压缩包只读，复制一份到项目 archives/ 下保存。
- 一层层解到底；加密、损坏、缺工具、分卷不全都登记为问题，绝不静默跳过。
- 每个文件按 SHA-256 存一份，每一处出处都保留（批次 / 压缩包 / 文件夹 / 文件）。
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import pdfpages
from .store import Project, sha256_file

MAX_DEPTH = 8
MAX_TOTAL_BYTES = 20 * 1024**3  # 单批次解压总量上限，防压缩炸弹
SEP = " / "

KIND_BY_EXT = {
    ".pdf": "pdf",
    ".dwg": "cad", ".dxf": "cad", ".dwf": "cad",
    ".doc": "office", ".docx": "office", ".xls": "office", ".xlsx": "office", ".wps": "office", ".et": "office",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".tif": "image", ".tiff": "image", ".bmp": "image",
    ".txt": "text", ".md": "text",
}
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz")
VOLUME_HINTS = (".part1.rar", ".part01.rar", ".z01", ".7z.001", ".zip.001")


@dataclass
class ReceiveResult:
    seq: int
    name: str
    files: int = 0
    by_kind: dict = field(default_factory=dict)
    nested_archives: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    duplicates: int = 0
    new_pages: int = 0


def _ext_of(name: str) -> str:
    low = name.lower()
    for e in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if low.endswith(e):
            return e
    return os.path.splitext(low)[1]


NOT_ARCHIVE_EXTS = (".docx", ".xlsx", ".pptx", ".dwfx", ".xps", ".ofd", ".odt", ".ods")  # 本身是 zip 格式的文档


def _archive_type(path: Path, name: str) -> str | None:
    if _ext_of(name) in NOT_ARCHIVE_EXTS:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if _ext_of(name) in (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz") and tarfile.is_tarfile(path):
        return "tar"
    return None


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """修复中文文件名乱码：未声明 UTF-8 的 zip（Windows 常见 GBK）按 GB18030 重新解码。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _safe_parts(name: str) -> list[str]:
    return [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]


class _Receiver:
    def __init__(self, proj: Project, con, seq: int, passwords: list[str]):
        self.proj, self.con, self.seq = proj, con, seq
        self.passwords = [p.encode() for p in passwords]
        self.total = 0
        self.result: ReceiveResult | None = None

    def issue(self, path: str, kind: str, detail: str) -> None:
        self.con.execute(
            "INSERT INTO extraction_issues(batch_seq, path, kind, detail) VALUES (?,?,?,?)",
            (self.seq, path, kind, detail),
        )
        self.result.issues.append({"path": path, "kind": kind, "detail": detail})

    # ---------- 入口 ----------
    def handle(self, disk: Path, display: str, depth: int) -> None:
        name = display.split(SEP)[-1]
        low = name.lower()
        if any(low.endswith(h) for h in VOLUME_HINTS) or low.endswith((".z02", ".7z.002", ".part2.rar", ".part02.rar")):
            self.issue(display, "volume", "分卷压缩包，需要所有分卷齐全并用专用工具合并后再收图")
            self._register(disk, display, depth, kind="archive")
            return
        atype = _archive_type(disk, name)
        if atype:
            self._register(disk, display, depth, kind="archive")
            if depth >= MAX_DEPTH:
                self.issue(display, "too_deep", f"嵌套超过 {MAX_DEPTH} 层，未继续解开")
                return
            if depth > 0:
                self.result.nested_archives.append(display)
            getattr(self, f"_open_{atype}")(disk, display, depth)
            return
        self._register(disk, display, depth)

    # ---------- 登记文件 ----------
    def _register(self, disk: Path, display: str, depth: int, kind: str | None = None) -> None:
        sha = sha256_file(disk)
        size = disk.stat().st_size
        ext = _ext_of(display.split(SEP)[-1])
        kind = kind or KIND_BY_EXT.get(ext, "other")
        exists = self.con.execute("SELECT 1 FROM files WHERE sha256=?", (sha,)).fetchone()
        dup_in_batch = self.con.execute(
            "SELECT 1 FROM occurrences WHERE sha256=? AND batch_seq=?", (sha, self.seq)
        ).fetchone()
        if dup_in_batch:
            self.result.duplicates += 1
        self.con.execute(
            "INSERT INTO occurrences(batch_seq, sha256, path, depth) VALUES (?,?,?,?)",
            (self.seq, sha, display, depth),
        )
        if kind != "archive":
            self.result.files += 1
            self.result.by_kind[kind] = self.result.by_kind.get(kind, 0) + 1
        if exists:
            return
        stored = self.proj.store_blob(disk, sha, ext)
        page_count = has_text = None
        attachments = []
        if kind == "pdf":
            try:
                pages, attachments = pdfpages.analyze_pdf(stored)
                page_count = len(pages)
                has_text = int(any(p.has_text for p in pages))
                for p in pages:
                    self.con.execute(
                        "INSERT OR IGNORE INTO pages(page_id, file_sha256, page_index, width, height, has_text,"
                        " render_hash, dhash, text) VALUES (?,?,?,?,?,?,?,?,?)",
                        (pdfpages.page_id(sha, p.page_index), sha, p.page_index, p.width, p.height,
                         int(p.has_text), p.render_hash, p.dhash, p.text),
                    )
                self.result.new_pages += page_count
            except PermissionError as e:
                self.issue(display, "encrypted", str(e))
            except Exception as e:  # noqa: BLE001 - 任何解析失败都登记，不中断收图
                self.issue(display, "corrupt", f"PDF 无法打开：{e}")
        self.con.execute(
            "INSERT INTO files(sha256, size, ext, kind, stored_path, page_count, has_text_layer) VALUES (?,?,?,?,?,?,?)",
            (sha, size, ext, kind, str(stored.relative_to(self.proj.root)), page_count, has_text),
        )
        for att_name, data in attachments:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / (_safe_parts(att_name) or ["附件"])[-1]
                p.write_bytes(data)
                self.handle(p, f"{display}{SEP}[附件] {p.name}", depth + 1)

    def _budget(self, n: int, display: str) -> bool:
        self.total += n
        if self.total > MAX_TOTAL_BYTES:
            self.issue(display, "too_large", "解压总量超过上限，疑似压缩炸弹，已停止")
            return False
        return True

    # ---------- 各类压缩包 ----------
    def _open_zip(self, disk: Path, display: str, depth: int) -> None:
        try:
            zf = zipfile.ZipFile(disk)
        except zipfile.BadZipFile as e:
            self.issue(display, "corrupt", f"压缩包损坏：{e}")
            return
        with zf, tempfile.TemporaryDirectory() as td:
            encrypted_reported = False
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = _safe_parts(_zip_member_name(info))
                if not parts:
                    continue
                member_display = display + SEP + SEP.join(parts)
                pwd = None
                if info.flag_bits & 0x1:
                    pwd = self._find_zip_password(zf, info)
                    if pwd is None:
                        if not encrypted_reported:
                            self.issue(display, "encrypted", "压缩包已加密，需要密码（bidbench receive --password ...）")
                            encrypted_reported = True
                        continue
                if not self._budget(info.file_size, display):
                    return
                out = Path(td, *parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info, pwd=pwd) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as e:
                    self.issue(member_display, "corrupt", f"无法解出：{e}")
                    continue
                self.handle(out, member_display, depth + 1)
                out.unlink(missing_ok=True)

    def _find_zip_password(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes | None:
        for pwd in self.passwords:
            try:
                with zf.open(info, pwd=pwd) as f:
                    f.read(1)
                return pwd
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError):
                continue
        return None

    def _open_tar(self, disk: Path, display: str, depth: int) -> None:
        try:
            tf = tarfile.open(disk)
        except tarfile.TarError as e:
            self.issue(display, "corrupt", f"压缩包损坏：{e}")
            return
        with tf, tempfile.TemporaryDirectory() as td:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                parts = _safe_parts(m.name)
                if not parts or not self._budget(m.size, display):
                    continue
                out = Path(td, *parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                self.handle(out, display + SEP + SEP.join(parts), depth + 1)
                out.unlink(missing_ok=True)

    def _open_7z(self, disk: Path, display: str, depth: int) -> None:
        try:
            import py7zr  # type: ignore
        except ImportError:
            self.issue(display, "no_tool", "7z 压缩包需要安装 py7zr（pip install bidbench[archives]）")
            return
        try:
            with py7zr.SevenZipFile(disk, password=(self.passwords[0].decode() if self.passwords else None)) as z, \
                    tempfile.TemporaryDirectory() as td:
                if z.needs_password() and not self.passwords:
                    self.issue(display, "encrypted", "压缩包已加密，需要密码")
                    return
                z.extractall(td)
                self._walk_dir(Path(td), display, depth)
        except Exception as e:  # noqa: BLE001
            self.issue(display, "corrupt", f"7z 解压失败：{e}")

    def _open_rar(self, disk: Path, display: str, depth: int) -> None:
        try:
            import rarfile  # type: ignore
        except ImportError:
            self.issue(display, "no_tool", "rar 压缩包需要安装 rarfile 及 unrar/bsdtar 工具")
            return
        try:
            with rarfile.RarFile(disk) as rf, tempfile.TemporaryDirectory() as td:
                if rf.needs_password() and not self.passwords:
                    self.issue(display, "encrypted", "压缩包已加密，需要密码")
                    return
                if self.passwords:
                    rf.setpassword(self.passwords[0].decode())
                rf.extractall(td)
                self._walk_dir(Path(td), display, depth)
        except Exception as e:  # noqa: BLE001
            self.issue(display, "no_tool" if "unrar" in str(e).lower() else "corrupt", f"rar 解压失败：{e}")

    def _walk_dir(self, root: Path, display: str, depth: int) -> None:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root).parts
                self.handle(p, display + SEP + SEP.join(rel), depth + 1)


def receive(proj: Project, archive: Path, received_at: str, name: str | None = None,
            passwords: list[str] | None = None, note: str | None = None) -> ReceiveResult:
    archive = Path(archive)
    with proj.connect() as con:
        seq = (con.execute("SELECT COALESCE(MAX(seq), 0) FROM batches").fetchone()[0]) + 1
        sha = sha256_file(archive)
        dup = con.execute("SELECT seq FROM batches WHERE archive_sha256=?", (sha,)).fetchone()
        if dup:
            raise ValueError(f"这个压缩包已经作为批次 #{dup['seq']} 收过了")
        stored = proj.path("archives", f"{seq:03d}-{archive.name}")
        shutil.copyfile(archive, stored)
        os.chmod(stored, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        bname = name or archive.stem
        con.execute(
            "INSERT INTO batches(seq, name, received_at, archive_name, archive_sha256, stored_path, note) VALUES (?,?,?,?,?,?,?)",
            (seq, bname, received_at, archive.name, sha, str(stored.relative_to(proj.root)), note),
        )
        r = _Receiver(proj, con, seq, passwords or [])
        r.result = ReceiveResult(seq=seq, name=bname)
        root_display = f"批次#{seq}{SEP}{archive.name}"
        if _archive_type(stored, archive.name):
            con.execute(
                "INSERT INTO occurrences(batch_seq, sha256, path, depth) VALUES (?,?,?,?)",
                (seq, sha, root_display, 0),
            )
            getattr(r, f"_open_{_archive_type(stored, archive.name)}")(stored, root_display, 0)
        else:
            r.handle(stored, root_display, 0)
        return r.result
