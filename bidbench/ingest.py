"""收图：登记批次、递归解包、按内容去重入库、登记解包问题。

一批图纸可以是一个压缩包、一个文件、同时收到的几个文件，或者一个文件夹（连同子文件夹）。

原则：
- 原始压缩包只读，复制一份到项目 archives/ 下保存；文件夹和多个文件逐个存进 blobs/，
  另在 archives/ 下留一份只读的文件清单，记下原来的位置和结构。
- 一层层解到底；加密、损坏、缺工具、分卷不全、读不出来都登记为问题，绝不静默跳过。
- 被杀毒软件拦下或解出后不见了的文件登记为“被拦截”，按高风险文件处理。
- 每个文件按 SHA-256 存一份，每一处出处都保留（批次 / 压缩包 / 文件夹 / 文件）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import extractors, pdfpages, risk
from .store import Project, sha256_file

MAX_DEPTH = 8
MAX_TOTAL_BYTES = 20 * 1024**3  # 单批次解压总量上限，防压缩炸弹
MAX_SOURCE_FILES = 20000        # 一个文件夹最多收这么多文件，防止选错文件夹（比如整个磁盘）
SEP = " / "
FOLDER_MARK = "（文件夹）"       # 文件夹批次出处路径里，文件夹名后面加这个标记
NEED_PASSWORD = "需要密码（工作台收图时填“压缩包密码”，命令行加 --password）"
NOISE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
VIRUS_ERRORS = {225, 226}  # Windows：文件含病毒 / 已被当作病毒删除
GONE_ERRORS = {2, 3}       # 找不到文件 / 找不到路径
LOCK_ERRORS = {32, 33}     # 文件正被其他程序占用

KIND_BY_EXT = {
    ".pdf": "pdf",
    ".dwg": "cad", ".dxf": "cad", ".dwf": "cad",
    ".doc": "office", ".docx": "office", ".xls": "office", ".xlsx": "office", ".wps": "office", ".et": "office",
    ".docm": "office", ".xlsm": "office", ".ppt": "office", ".pptx": "office", ".pptm": "office",
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
    risky: set = field(default_factory=set)  # 有风险的文件（SHA-256）
    source: str = "archive"                  # archive / file / folder / files
    skipped: list = field(default_factory=list)  # 没收的系统文件（Thumbs.db、Office 临时锁文件等）


def _ext_of(name: str) -> str:
    low = name.lower()
    for e in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if low.endswith(e):
            return e
    return os.path.splitext(low)[1]


NOT_ARCHIVE_EXTS = (".docx", ".xlsx", ".pptx", ".dwfx", ".xps", ".ofd", ".odt", ".ods",  # 本身是 zip 格式的文档
                    ".docm", ".dotx", ".dotm", ".xlsm", ".xltx", ".xltm", ".xlam", ".pptm", ".potx", ".potm",
                    ".ppsx", ".ppsm", ".ppam", ".vsdx", ".vsdm", ".jar")


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


def _is_noise(name: str) -> bool:
    """系统和 Office 自动生成的文件：缩略图缓存、文件夹设置、打开文档时留下的临时锁文件（~$开头）。"""
    return name.lower() in NOISE_NAMES or name.startswith("~$")


def _long(p: Path) -> Path:
    """Windows：换成不受 260 个字符限制的写法，深层文件夹里的长文件名也读得到。"""
    s = str(Path(p).resolve())
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return Path(s)
    return Path("\\\\?\\UNC\\" + s[2:]) if s.startswith("\\\\") else Path("\\\\?\\" + s)


def _plain(p: Path) -> str:
    """去掉 _long 加的前缀，用来比较路径。"""
    s = str(p)
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(s)


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(p))


def _size(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def _win_open_error(path: Path) -> int | None:
    """Windows：再试着打开一次，拿到系统给的确切原因（Python 的 open 会把“含病毒”笼统地报成参数错误）。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)  # 只读、允许他人读写删、打开已有文件
    if h is None or h == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error()
    k32.CloseHandle(h)
    return 0


def _unreadable(path: Path, err: OSError, source: bool) -> tuple[str, str]:
    """文件读不出来时的问题类型和说明。source 为 True 表示是收图来源里原有的文件，False 表示是刚解出来的。"""
    code = getattr(err, "winerror", None) or _win_open_error(path)
    if code in VIRUS_ERRORS:
        return "blocked", "Windows 报告这个文件含有病毒或可能不需要的软件，已被本机杀毒软件拦截，没有收进项目"
    gone = isinstance(err, FileNotFoundError) or code in GONE_ERRORS
    if not source:  # 刚解出来就读不了或不见了，多半是杀毒软件的实时防护
        return "blocked", ("解出后马上不见了" if gone else "解出后读不出来") + "，多半被本机杀毒软件删除或隔离，没有收进项目"
    if gone:
        return "unreadable", "收图过程中这个文件不见了（可能被移走、删除，或被杀毒软件隔离），没能收进来"
    if code in LOCK_ERRORS or isinstance(err, PermissionError):
        return "unreadable", "文件正被其他程序占用，或者没有读取权限，没能收进来；关掉占用它的程序后重新收图"
    return "unreadable", f"文件读不出来（系统提示：{err.strerror or err}），没能收进来"


class _Receiver:
    def __init__(self, proj: Project, con, seq: int, passwords: list[str]):
        self.proj, self.con, self.seq = proj, con, seq
        self.passwords = [p.encode() for p in passwords]
        self.total = 0
        self.result: ReceiveResult | None = None
        self.known_sha: dict[str, str | None] = {}  # 收文件夹时先算好的 SHA-256，登记时不用再读一遍

    def issue(self, path: str, kind: str, detail: str) -> None:
        self.con.execute(
            "INSERT INTO extraction_issues(batch_seq, path, kind, detail) VALUES (?,?,?,?)",
            (self.seq, path, kind, detail),
        )
        self.result.issues.append({"path": path, "kind": kind, "detail": detail})

    # ---------- 入口 ----------
    def handle(self, disk: Path, display: str, depth: int, source: bool = False) -> None:
        try:
            self._handle(disk, display, depth)
        except OSError as e:
            self.issue(display, *_unreadable(disk, e, source))

    def _handle(self, disk: Path, display: str, depth: int) -> None:
        name = display.split(SEP)[-1]
        low = name.lower()
        if any(low.endswith(h) for h in VOLUME_HINTS) or low.endswith((".z02", ".7z.002", ".part2.rar", ".part02.rar")):
            self.issue(display, "volume", "分卷压缩包，需要所有分卷齐全并用专用工具合并后再收图")
            self._register(disk, display, depth, kind="archive")
            return
        atype = _archive_type(disk, name)
        if atype:
            stored = self._register(disk, display, depth, kind="archive")
            if depth >= MAX_DEPTH:
                self.issue(display, "too_deep", f"嵌套超过 {MAX_DEPTH} 层，未继续解开")
                return
            if depth > 0:
                self.result.nested_archives.append(display)
            getattr(self, f"_open_{atype}")(stored, display, depth)  # 从项目里存的那份打开，内容一样、路径短
            return
        self._register(disk, display, depth)

    # ---------- 登记文件 ----------
    def _register(self, disk: Path, display: str, depth: int, kind: str | None = None) -> Path:
        """登记一处出处，内容第一次出现时存进 blobs。返回项目里存的那份文件。"""
        sha = self.known_sha.pop(str(disk), None) or sha256_file(disk)
        size = disk.stat().st_size
        ext = _ext_of(display.split(SEP)[-1])
        kind = kind or KIND_BY_EXT.get(ext, "other")
        exists = self.con.execute("SELECT stored_path FROM files WHERE sha256=?", (sha,)).fetchone()
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
            self._risk(sha, display, risk.check_name(display.split(SEP)[-1], risk.read_head(disk)))
        if exists:
            return self.proj.root / exists["stored_path"]
        stored = self.proj.store_blob(disk, sha, ext)
        if kind != "archive":
            self._risk(sha, display, risk.check_content(stored, ext, kind))
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
        return stored

    def _risk(self, sha: str, display: str, findings: list) -> None:
        risk.record(self.con, self.seq, sha, display, "static", findings)
        if findings:
            self.result.risky.add(sha)

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
                            self.issue(display, "encrypted", "压缩包已加密，" + NEED_PASSWORD)
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
            import py7zr  # type: ignore  # 可选：装了就优先用（支持加密 7z，包括连文件名都加密的）
        except ImportError:
            py7zr = None
        if py7zr is None or not self._open_7z_py(py7zr, disk, display, depth):
            self._open_external(disk, display, depth, "7z")

    def _open_7z_py(self, py7zr, disk: Path, display: str, depth: int) -> bool:
        """用 py7zr 解。解开或登记了加密问题时返回 True；py7zr 处理不了（压缩方式不支持等）返回 False，交给系统工具。"""
        pwds = [p.decode() for p in self.passwords]
        listing = None
        names_locked = False  # 连文件名都加密了：没有对的密码，连文件清单都读不出来
        for pwd in [None, *pwds]:
            try:
                with py7zr.SevenZipFile(disk, password=pwd) as z:
                    listing = (pwd, z.needs_password(), [f for f in z.list() if not f.is_directory])
                break
            except py7zr.exceptions.PasswordRequired:
                names_locked = True
            except Exception:  # noqa: BLE001 - 文件名加密时密码不对会报各种奇怪的错，换下一个密码
                if not names_locked:
                    return False
        if listing is None:
            self.issue(display, "encrypted", "提供的密码都不对，压缩包未能解开（这个压缩包连文件名都加密了）" if pwds
                       else "压缩包已加密，连文件名都加密了，" + NEED_PASSWORD)
            return True
        pwd, needs_pw, files = listing
        if needs_pw and not pwds:
            self.issue(display, "encrypted", "压缩包已加密，" + NEED_PASSWORD)
            return True
        if not self._budget(sum(f.uncompressed for f in files), display):
            return True
        names = [f.filename for f in files]
        for p in ([pwd] if names_locked else pwds if needs_pw else [None]):
            with tempfile.TemporaryDirectory() as td:
                try:
                    with py7zr.SevenZipFile(disk, password=p) as z:
                        z.extractall(td)
                except py7zr.exceptions.UnsupportedCompressionMethodError:
                    return False
                except OSError:  # 写到一半被拦（如杀毒软件），已解出的照常登记，少了的会在核对时标出
                    self._walk_dir(Path(td), display, depth, expected=names)
                    return True
                except Exception:  # noqa: BLE001 - 密码不对，内容解不出来，换下一个
                    if not needs_pw:
                        return False
                    continue
                self._walk_dir(Path(td), display, depth, expected=names)
                return True
        self.issue(display, "encrypted", "提供的密码都不对，压缩包未能解开")
        return True

    def _open_rar(self, disk: Path, display: str, depth: int) -> None:
        self._open_external(disk, display, depth, "rar")

    def _open_external(self, disk: Path, display: str, depth: int, kind: str) -> None:
        size = extractors.listed_size(disk)
        if size is not None and not self._budget(size, display):
            return
        with tempfile.TemporaryDirectory() as td:
            out = extractors.extract(disk, Path(td), kind, [p.decode() for p in self.passwords])
            if out.ok:
                expected = extractors.count_files(disk, out.tool, out.password)
                self._walk_dir(Path(td), display, depth, expected_count=expected)
                return
        if not out.tool:
            self.issue(display, "no_tool", out.message)
        elif out.encrypted and self.passwords:
            self.issue(display, "encrypted", f"压缩包已加密，提供的密码没能解开，或本机解压工具不支持这种加密方式。{out.tool}：{out.message}")
        elif out.encrypted:
            self.issue(display, "encrypted", f"压缩包已加密，{NEED_PASSWORD}。{out.tool}：{out.message}")
        else:
            self.issue(display, "corrupt", f"{out.tool} 解压失败，可能是加密、分卷不全或文件损坏：{out.message}")

    def _walk_dir(self, root: Path, display: str, depth: int, expected: list[str] | None = None,
                  expected_count: int | None = None) -> None:
        """登记解出的普通文件。符号链接一律跳过，防止借链接读到压缩包以外的文件。

        再和压缩包的文件清单（expected）或文件数（expected_count）核对：解出后不见了的，
        多半是被杀毒软件的实时防护删掉了，登记为“被拦截”，不能静默漏掉。
        """
        root_r = root.resolve()
        skipped: set[str] = set()
        found: set[str] = set()
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).parts
            if p.is_symlink():
                skipped.add("/".join(rel))
                continue
            if not p.is_file():
                continue
            try:
                p.resolve().relative_to(root_r)
            except ValueError:
                skipped.add("/".join(rel))
                continue
            found.add("/".join(rel))
            self.handle(p, display + SEP + SEP.join(rel), depth + 1)
        if skipped:
            self.issue(display, "symlink", f"压缩包里有 {len(skipped)} 个符号链接，出于安全考虑已跳过")
        if expected is not None:
            for name in expected:
                parts = _safe_parts(name)
                key = "/".join(parts)
                if parts and key not in found and key not in skipped:
                    self.issue(display + SEP + SEP.join(parts), "blocked",
                               "解压后没有找到这个文件，多半被本机杀毒软件删除或隔离（也可能是文件名里有本机不支持的字符），没有收进项目")
        elif expected_count is not None and len(found) < expected_count:  # 清单只数普通文件，链接不算
            n = expected_count - len(found)
            self.issue(display, "blocked", f"压缩包里有 {expected_count} 个文件，解出后少了 {n} 个，"
                                           "多半被本机杀毒软件删除或隔离，没有收进项目")


def receive(proj: Project, source, received_at: str, name: str | None = None,
            passwords: list[str] | None = None, note: str | None = None, progress=None) -> ReceiveResult:
    """收一批图纸。source 是一个压缩包或文件、一个文件夹，或者同时收到的几个文件（列表）。

    progress(已处理, 总数) 只在收文件夹和多个文件时调用。
    """
    sources = [Path(s) for s in source] if isinstance(source, (list, tuple)) else [Path(source)]
    if not sources:
        raise ValueError("请先选择压缩包、文件或文件夹")
    for s in sources:
        if not s.exists():
            raise FileNotFoundError(f"找不到 {s}")
    if len(sources) == 1 and sources[0].is_file():
        return _receive_file(proj, sources[0], received_at, name, passwords or [], note)
    return _receive_collection(proj, sources, received_at, name, passwords or [], note, progress)


def _receive_file(proj: Project, archive: Path, received_at: str, name: str | None, passwords: list[str],
                  note: str | None) -> ReceiveResult:
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
        r = _Receiver(proj, con, seq, passwords)
        r.result = ReceiveResult(seq=seq, name=bname)
        root_display = f"批次#{seq}{SEP}{archive.name}"
        if _archive_type(stored, archive.name):
            con.execute(
                "INSERT INTO occurrences(batch_seq, sha256, path, depth) VALUES (?,?,?,?)",
                (seq, sha, root_display, 0),
            )
            getattr(r, f"_open_{_archive_type(stored, archive.name)}")(stored, root_display, 0)
        else:
            r.result.source = "file"
            r.handle(stored, root_display, 0)
        return r.result


def _collect(proj: Project, sources: list[Path]) -> tuple[str, list[tuple[Path, list[str]]], list[str], int, list]:
    """列出这一批要收的文件。

    返回 (folder / files, [(磁盘路径, 出处路径的各段)], 跳过的系统文件, 跳过的链接数, 打不开的子文件夹)。
    文件夹里的符号链接和目录联接不跟进去；项目自己的目录在里面时整个跳过。
    """
    root = _plain(proj.root.resolve())
    if len(sources) == 1 and sources[0].is_dir():
        top = _long(sources[0])
        if _plain(top) == root or _plain(top).startswith(root + os.sep):
            raise ValueError("这个文件夹在项目目录里面，不能当作收图来源")
        entries, noise, links, unlisted = [], [], 0, []
        for dirpath, dirnames, filenames in os.walk(top, onerror=unlisted.append):
            here = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                sub = here / d
                if _is_link(sub):
                    links += 1
                elif _plain(sub) != root:  # 项目目录在这个文件夹里：不收项目自己的数据
                    keep.append(d)
            dirnames[:] = keep
            for f in sorted(filenames):
                p = here / f
                rel = list(p.relative_to(top).parts)
                if _is_link(p):
                    links += 1
                elif _is_noise(f):
                    noise.append("/".join(rel))
                else:
                    entries.append((p, rel))
        return "folder", entries, noise, links, unlisted
    if any(p.is_dir() for p in sources):
        raise ValueError("一次只能收一个文件夹；多个文件可以一起收")
    for p in sources:
        if _plain(_long(p)).startswith(root + os.sep):
            raise ValueError(f"{p.name} 在项目目录里面，不能当作收图来源")
    return "files", [(_long(p), [p.name]) for p in sources], [], 0, []


def _receive_collection(proj: Project, sources: list[Path], received_at: str, name: str | None,
                        passwords: list[str], note: str | None, progress) -> ReceiveResult:
    kind, entries, noise, links, unlisted = _collect(proj, sources)
    folder = sources[0].resolve() if kind == "folder" else None
    if not entries:
        raise ValueError("这个文件夹里没有可收的文件" if folder else "没有选中文件")
    if len(entries) > MAX_SOURCE_FILES:
        raise ValueError(f"文件夹里有 {len(entries)} 个文件，超过 {MAX_SOURCE_FILES} 个，请确认选对了文件夹")
    sizes = {str(p): _size(p) for p, _ in entries}
    total = sum(s or 0 for s in sizes.values())
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"文件夹里的文件共 {total / 1024**3:.1f} GB，超过上限，请确认选对了文件夹")

    # 先算好每个文件的指纹：整批内容和以前某一批完全一样时直接拒收
    shas: dict[str, str | None] = {}
    for p, _ in entries:
        try:
            shas[str(p)] = sha256_file(p)
        except OSError:
            shas[str(p)] = None  # 登记时会再读一次，按原因记成“读不出来”或“被拦截”
    listing = sorted(("/".join(rel), shas[str(p)] or "?") for p, rel in entries)
    fingerprint = hashlib.sha256("\n".join(f"{a}\t{b}" for a, b in listing).encode("utf-8")).hexdigest()

    with proj.connect() as con:
        seq = (con.execute("SELECT COALESCE(MAX(seq), 0) FROM batches").fetchone()[0]) + 1
        dup = con.execute("SELECT seq FROM batches WHERE archive_sha256=?", (fingerprint,)).fetchone()
        if dup:
            raise ValueError(f"{'这个文件夹里的文件' if folder else '这几个文件'}和批次 #{dup['seq']} 收到的完全一样，已经收过了")
        if folder:
            bname, source_name = name or folder.name, folder.name + FOLDER_MARK
        else:
            bname = name or f"{sources[0].stem} 等 {len(sources)} 个文件"
            source_name = "、".join(p.name for p in sources)
            source_name = source_name if len(source_name) <= 200 else source_name[:199] + "…"
        safe = re.sub(r'[\\/:*?"<>|]+', "_", bname)[:60]
        manifest = proj.path("archives", f"{seq:03d}-{safe}-文件清单.json")
        manifest.write_text(json.dumps({
            "kind": kind, "sources": [str(s.resolve()) for s in sources], "received_at": received_at,
            "files": [{"path": "/".join(rel), "size": sizes[str(p)], "sha256": shas[str(p)]} for p, rel in entries],
            "skipped": noise,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(manifest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        con.execute(
            "INSERT INTO batches(seq, name, received_at, archive_name, archive_sha256, stored_path, note) VALUES (?,?,?,?,?,?,?)",
            (seq, bname, received_at, source_name, fingerprint, str(manifest.relative_to(proj.root)), note),
        )
        r = _Receiver(proj, con, seq, passwords)
        r.result = ReceiveResult(seq=seq, name=bname, source=kind, skipped=noise)
        root_display = f"批次#{seq}" + (f"{SEP}{folder.name}{FOLDER_MARK}" if folder else "")
        for i, (p, rel) in enumerate(entries):
            if progress:
                progress(i, len(entries))
            r.known_sha[str(p)] = shas[str(p)]
            r.handle(p, root_display + SEP + SEP.join(rel), 0, source=True)
        if progress:
            progress(len(entries), len(entries))
        if links:
            r.issue(root_display, "symlink", f"文件夹里有 {links} 个快捷链接（符号链接或目录联接），出于安全考虑没有跟进去")
        for err in unlisted:
            where = Path(getattr(err, "filename", "") or "")
            try:
                rel = list(where.relative_to(_long(folder)).parts) if folder else []
            except ValueError:
                rel = [where.name]
            r.issue(root_display + "".join(SEP + x for x in rel), "unreadable",
                    "这个子文件夹打不开（多半是没有权限），里面的文件没能收进来")
        return r.result
