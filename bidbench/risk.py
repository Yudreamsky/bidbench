"""风险文件检查：收图时找出可能带病毒或会自动运行代码的文件。

只看文件名和内容特征，从不打开、加载或运行文件。查出的问题登记在 risk_findings 表里，
由人在问题中心决定“确认安全”还是“确认有风险”。

检查项：
- CAD 自动加载脚本：acad.lsp、acaddoc.lsp、acad.fas、*.mnl 等（AutoCAD、中望、浩辰打开同目录图纸时会自动运行）
- 其他 CAD 插件和脚本：.lsp .fas .vlx .dvb .arx 等；LISP 源码里有复制文件、写注册表、启动程序等操作时升为高风险
- 可执行文件、双扩展名（图纸.pdf.exe）、文件名里的反向显示字符、扩展名和实际内容不符（.pdf 实为程序）；
  反向显示字符出现在内容和扩展名对得上的图纸（DWG、PDF、图片）上时只算低风险：它没法运行，只是名字不干净
- 带宏的 Office 文件、会从外部地址加载模板的 Office 文件
- PDF 里的 JavaScript、启动外部程序（Launch）、多媒体（RichMedia / Flash）
"""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

RULES_VERSION = 2
LEVEL_WORD = {"high": "高", "medium": "中", "low": "低"}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}
BLOCKED_SOURCE = "本机杀毒软件"

EXEC_EXTS = {
    ".exe", ".com", ".scr", ".pif", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1",
    ".msi", ".msp", ".hta", ".cpl", ".lnk", ".jar", ".dll", ".sys", ".reg", ".chm", ".msc", ".scf", ".application",
}
CAD_CODE_EXTS = {".lsp", ".fas", ".vlx", ".mnl", ".dvb", ".arx", ".crx", ".dbx", ".zrx", ".grx", ".brx"}
CAD_AUTOLOAD = re.compile(r"^(acad|zwcad|gcad)\w*\.(lsp|fas|vlx|mnl|dvb|rx)$", re.I)
MACRO_EXTS = {".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".xla", ".pptm", ".potm", ".ppam", ".ppsm", ".sldm"}
OOXML_EXTS = {".docx", ".xlsx", ".pptx", ".dotx", ".xltx", ".potx", ".ppsx", ".vsdx", ".vsdm"} | MACRO_EXTS - {".xla"}
BIDI = re.compile("[‪-‮⁦-⁩‎‏]")
# 这些格式本身不能运行，开头几个字节对得上就确实是这种文件
BENIGN_MAGIC = {".dwg": (b"AC1",), ".pdf": (b"%PDF",), ".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8\xff",),
                ".jpeg": (b"\xff\xd8\xff",), ".tif": (b"II*\0", b"MM\0*"), ".tiff": (b"II*\0", b"MM\0*")}
LISP_SIGNS = {
    "vl-file-copy": "复制文件", "vl-file-delete": "删除文件", "vl-file-rename": "改文件名",
    "vl-registry-write": "写注册表", "vl-registry-delete": "删注册表", "startapp": "启动外部程序",
    "wscript.shell": "调用系统命令", "shell.application": "调用系统命令", "scripting.filesystemobject": "操作文件系统",
    "acaddoc.lsp": "引用自动加载文件名", "acad.lsp": "引用自动加载文件名", "acad.fas": "引用自动加载文件名",
}
PDF_RULES = [
    (re.compile(r"/(JavaScript|JS)\b"), "high", "pdf_script", "含 JavaScript 脚本，用 Acrobat 等阅读器打开时可能自动运行"),
    (re.compile(r"/Launch\b"), "high", "pdf_launch", "含“启动外部程序”动作，点击或打开时可能运行别的文件"),
    (re.compile(r"/(RichMedia|Flash)\b"), "medium", "pdf_media", "含 Flash / 多媒体内容，旧版阅读器有漏洞"),
]
CLEANABLE = ("pdf_script", "pdf_launch", "pdf_media")  # 导出时可以去除的 PDF 内容


@dataclass
class Finding:
    level: str   # high / medium / low
    kind: str
    detail: str


def _ext(name: str) -> str:
    return os.path.splitext(name.lower())[1].strip()


def read_head(path: Path, n: int = 64) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def _content_type(head: bytes) -> str | None:
    if head[:2] == b"MZ" or head[:4] == b"\x7fELF":
        return "program"
    if b"FAS4-FILE" in head[:64] or head.startswith(b"VRTLIB-1"):
        return "cad_compiled"
    return None


def _matches_ext(ext: str, head: bytes) -> bool:
    return any(head.startswith(m) for m in BENIGN_MAGIC.get(ext, ()))


def check_name(name: str, head: bytes) -> list[Finding]:
    """按文件名和文件开头几个字节检查。同一份内容换个名字出现，要重新查一次。"""
    out: list[Finding] = []
    ext = _ext(name)
    if BIDI.search(name):
        if _matches_ext(ext, head) and _ext(BIDI.sub("", name)) == ext:
            out.append(Finding("low", "bidi_name_benign", f"文件名里有看不见的方向控制字符，但内容确实是 {ext} 文件，"
                                                          "不能运行；常见于外方系统导出的文件名，只是名字不干净"))
        else:
            out.append(Finding("high", "bidi_name", "文件名里有反向显示字符，显示出来的扩展名是假的"))
    stem_ext = _ext(os.path.splitext(name)[0])
    real = _content_type(head)
    if ext == ".scr" and real != "program":
        out.append(Finding("medium", "cad_script", "AutoCAD 命令脚本（.scr），执行后会自动跑一串 CAD 命令"))
    elif ext in EXEC_EXTS:
        if stem_ext and stem_ext not in EXEC_EXTS and len(stem_ext) <= 6:
            out.append(Finding("high", "double_ext", f"双扩展名：看起来是 {stem_ext} 文件，实际是 {ext} 程序，双击就会运行"))
        else:
            out.append(Finding("high", "executable", f"可执行文件（{ext}），投标图纸里一般不该有，双击就会运行"))
    if CAD_AUTOLOAD.match(name.strip()) or ext == ".mnl":
        out.append(Finding("high", "cad_autoload", "CAD 自动加载脚本：AutoCAD（及中望、浩辰）打开同一文件夹里的图纸时会自动运行它，"
                                                   "是 CAD 病毒最常见的传播方式"))
    elif ext in CAD_CODE_EXTS:
        out.append(Finding("medium", "cad_script", f"CAD 插件或脚本（{ext}），加载后可以执行任意操作；不会自动运行，但不要随手加载"))
    if real == "program" and ext not in EXEC_EXTS:
        out.append(Finding("high", "disguised", f"扩展名是 {ext or '（无）'}，实际内容是可执行程序，属于伪装"))
    if real == "cad_compiled" and ext not in CAD_CODE_EXTS:
        out.append(Finding("high", "disguised", f"扩展名是 {ext or '（无）'}，实际内容是编译过的 CAD 脚本，属于伪装"))
    if ext in MACRO_EXTS:
        out.append(Finding("medium", "macro_format", f"启用宏的 Office 格式（{ext}），打开时不要点“启用宏”"))
    return out


def check_content(path: Path, ext: str, kind: str) -> list[Finding]:
    """按文件内容检查。每份内容只查一次。"""
    head = read_head(path, 8)
    try:
        if ext in (".lsp", ".mnl"):
            return _check_lisp(path)
        if head.startswith(b"PK\x03\x04") and ext in OOXML_EXTS:
            return _check_ooxml(path)
        if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return _check_ole(path)
        if kind == "pdf":
            return pdf_findings(path)
    except Exception as e:  # noqa: BLE001 - 检查失败不影响收图，但要记下来
        return [Finding("low", "unchecked", f"内容检查没能完成：{e}")]
    return []


def _check_lisp(path: Path) -> list[Finding]:
    text = path.read_bytes()[: 4 << 20].decode("gb18030", errors="ignore").lower()
    hits = sorted({label for sign, label in LISP_SIGNS.items() if sign in text})
    if not hits:
        return []
    return [Finding("high", "lisp_suspicious", f"脚本里有{'、'.join(hits)}等操作，常见于自我复制的 CAD 病毒")]


def _check_ooxml(path: Path) -> list[Finding]:
    out = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            out.append(Finding("high", "macro", "含 VBA 宏代码，启用宏后会运行"))
        for n in names:
            if not n.endswith(".rels"):
                continue
            data = z.read(n)[: 1 << 20]
            if b'TargetMode="External"' in data and re.search(rb"attachedTemplate|oleObject|/frame", data):
                out.append(Finding("high", "remote_template", "打开时会从外部地址加载模板或对象（常见的钓鱼手法）"))
                break
        if any(n.lower().startswith(("word/activex", "xl/activex", "ppt/activex")) for n in names):
            out.append(Finding("medium", "activex", "含 ActiveX 控件"))
    return out


def _check_ole(path: Path) -> list[Finding]:
    data = path.read_bytes()[: 200 << 20]
    if "_VBA_PROJECT".encode("utf-16-le") in data or b"_VBA_PROJECT" in data:
        return [Finding("high", "macro", "含 VBA 宏代码，启用宏后会运行")]
    return []


def pdf_findings(src) -> list[Finding]:
    """逐个检查 PDF 对象（含压缩对象流里的对象）。src 可以是路径或字节。"""
    import pymupdf

    doc = pymupdf.open(stream=src, filetype="pdf") if isinstance(src, bytes) else pymupdf.open(src)
    try:
        if doc.needs_pass and not doc.authenticate(""):
            return [Finding("low", "unchecked", "PDF 加了密码，无法检查里面有没有脚本")]
        found: dict[str, Finding] = {}
        for x in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(x, compressed=True)
            except Exception:  # noqa: BLE001 - 损坏的对象跳过
                continue
            for rx, level, kind, detail in PDF_RULES:
                if kind not in found and rx.search(obj):
                    found[kind] = Finding(level, kind, detail)
        return list(found.values())
    finally:
        doc.close()


# ---------------- 登记 ----------------

def record(con, batch_seq: int, sha: str, path: str, source: str, findings: list[Finding]) -> None:
    for f in findings:
        con.execute("INSERT INTO risk_findings(batch_seq, sha256, path, source, level, kind, detail) VALUES (?,?,?,?,?,?,?)",
                    (batch_seq, sha, path, source, f.level, f.kind, f.detail))


def needs_cleaning(con, sha: str) -> bool:
    q = "SELECT 1 FROM risk_findings WHERE sha256=? AND kind IN (%s) LIMIT 1" % ",".join("?" * len(CLEANABLE))
    return con.execute(q, (sha, *CLEANABLE)).fetchone() is not None


def rescan_static(proj) -> int:
    """用当前规则重新检查已收的全部文件（老项目、规则更新后用）。返回有风险的文件数。"""
    with proj.connect() as con:
        con.execute("DELETE FROM risk_findings WHERE source='static'")
        files = {r["sha256"]: r for r in con.execute("SELECT * FROM files")}
        seen = set()
        for o in con.execute("SELECT * FROM occurrences ORDER BY batch_seq, id").fetchall():
            f = files.get(o["sha256"])
            if f is None or f["kind"] == "archive":
                continue
            blob = proj.root / f["stored_path"]
            record(con, o["batch_seq"], o["sha256"], o["path"], "static", check_name(o["path"].split(" / ")[-1], read_head(blob)))
            if o["sha256"] not in seen:
                seen.add(o["sha256"])
                if blob.exists():
                    record(con, o["batch_seq"], o["sha256"], o["path"], "static", check_content(blob, f["ext"], f["kind"]))
        return con.execute("SELECT COUNT(DISTINCT sha256) FROM risk_findings").fetchone()[0]


def blocked_key(batch_seq: int, path: str) -> str:
    """被拦下的文件读不到内容，按批次和出处算一个固定的编号，和普通风险文件一样取前 12 位用来做决定。"""
    return hashlib.sha256(f"blocked\n{batch_seq}\n{path}".encode("utf-8")).hexdigest()


def blocked_rows(issues) -> list[dict]:
    """收图时被杀毒软件拦下、或解出后不见了的文件（解包问题里的 blocked），当作高风险文件。"""
    return [{"batch_seq": i["batch_seq"], "sha256": blocked_key(i["batch_seq"], i["path"]), "path": i["path"],
             "source": BLOCKED_SOURCE, "level": "high", "kind": "av_blocked", "detail": i["detail"]}
            for i in issues if i["kind"] == "blocked"]


def batch_summary(proj, seq: int) -> dict:
    """某一批次的风险文件和杀毒扫描结果，给解包报告和命令行用。"""
    with proj.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM risk_findings WHERE batch_seq=? ORDER BY id", (seq,))]
        rows += blocked_rows(dict(r) for r in con.execute("SELECT * FROM extraction_issues WHERE batch_seq=? ORDER BY id", (seq,)))
        rows.sort(key=lambda r: LEVEL_ORDER[r["level"]])  # 每个文件最严重的发现排在前面
        scans = [dict(r) for r in con.execute("SELECT * FROM av_scans WHERE batch_seq=? ORDER BY id", (seq,))]
    by_file: dict[str, dict] = {}
    for r in rows:
        f = by_file.setdefault(r["sha256"], {"sha": r["sha256"], "path": r["path"], "level": r["level"], "details": []})
        if LEVEL_ORDER[r["level"]] < LEVEL_ORDER[f["level"]]:
            f["level"] = r["level"]
        if r["detail"] not in f["details"]:
            f["details"].append(r["detail"])
    files = sorted(by_file.values(), key=lambda f: (LEVEL_ORDER[f["level"]], f["path"]))
    return {"files": files, "scans": scans}
