"""调用本机杀毒软件扫描收到的文件：Windows 上用系统自带的 Windows Defender，其他系统用 ClamAV（如果装了）。

不自带病毒库，也不联网。扫描时只报告、不删除（Defender 加 -DisableRemediation），
是否删除由人决定；原件始终在项目的 archives/ 里。
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .debuglog import safe_command, tail

TIMEOUT = 3600
log = logging.getLogger(__name__)
SHA_RE = re.compile(r"([0-9a-f]{64})")


@dataclass
class Engine:
    name: str      # 显示名
    kind: str      # defender / clamav
    path: str


@dataclass
class ScanResult:
    engine: str
    ok: bool
    scanned: int = 0
    threats: dict = field(default_factory=dict)   # sha -> [威胁名]
    vanished: list = field(default_factory=list)  # 入库后不见了的文件
    message: str = ""


def find_engine() -> Engine | None:
    if os.name == "nt":
        cands = sorted(glob.glob(os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                                              "Microsoft", "Windows Defender", "Platform", "*", "MpCmdRun.exe")), reverse=True)
        cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe"))
        for c in cands:
            if os.path.isfile(c):
                return Engine("Windows Defender", "defender", c)
    for exe in ("clamscan",):
        p = shutil.which(exe)
        if not p and os.name == "nt":
            p = next((c for c in (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ClamAV", "clamscan.exe"),)
                      if os.path.isfile(c)), None)
        if p:
            return Engine("ClamAV", "clamav", p)
    return None


def _run(cmd: list[str]) -> tuple[int, str]:
    t = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("杀毒软件没能运行：%s：%s", safe_command(cmd), e)
        raise
    text = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    log.info("杀毒扫描：%s → 返回 %s，%.1f 秒", safe_command(cmd), r.returncode, time.monotonic() - t)
    if r.returncode != 0:
        log.info("杀毒软件的输出：%s", tail(text))
    return r.returncode, text


def _command(engine: Engine, target: Path) -> list[str]:
    if engine.kind == "defender":
        return [engine.path, "-Scan", "-ScanType", "3", "-File", str(target), "-DisableRemediation"]
    return [engine.path, "--infected", "--no-summary", "-r", str(target)]


def parse_output(engine: Engine, text: str) -> dict[str, list[str]]:
    """从扫描输出里找出有问题的文件（暂存时文件名就是 SHA-256）和威胁名。"""
    out: dict[str, list[str]] = {}
    if engine.kind == "defender":
        threat = None
        for ln in text.splitlines():
            m = re.match(r"\s*Threat\s*:\s*(.+)", ln)
            if m:
                threat = m.group(1).strip()
                continue
            m = re.match(r"\s*file\s*:\s*(.+)", ln, re.I)
            if m and threat:
                sha = SHA_RE.search(m.group(1).split("->")[0].lower())
                if sha:
                    out.setdefault(sha.group(1), []).append(threat)
    else:
        for ln in text.splitlines():
            m = re.match(r"(.+?):\s+(.+)\s+FOUND\s*$", ln)
            if m:
                sha = SHA_RE.search(m.group(1).lower())
                if sha:
                    out.setdefault(sha.group(1), []).append(m.group(2).strip())
    return out


def _found_any(engine: Engine, code: int, text: str = "") -> bool | None:
    """返回码：True 发现威胁，False 干净，None 扫描出错。"""
    if engine.kind == "defender":
        if "Failed with hr" in text:
            return None
        return {0: False, 2: True}.get(code)
    return {0: False, 1: True}.get(code)


def scan(engine: Engine, files: dict[str, Path], workdir: Path) -> ScanResult:
    """把要扫的文件以硬链接（不行就复制）集中到一个临时目录里扫一次，再按文件名对回 SHA-256。"""
    res = ScanResult(engine=engine.name, ok=False)
    stage = workdir / f".scan-{datetime.now():%Y%m%d%H%M%S}-{os.getpid()}"
    stage.mkdir(parents=True)
    try:
        staged: dict[str, Path] = {}
        for sha, src in files.items():
            if not src.exists():
                res.vanished.append(sha)
                continue
            dst = stage / (sha + src.suffix)
            try:
                os.link(src, dst)
            except OSError:
                try:
                    shutil.copyfile(src, dst)
                except OSError:
                    res.vanished.append(sha)  # 读不出来，多半被实时防护拦了
                    continue
            staged[sha] = dst
        res.scanned = len(staged)
        if not staged:
            res.ok = True
            return res
        try:
            code, text = _run(_command(engine, stage))
        except (OSError, subprocess.SubprocessError) as e:
            res.message = f"{engine.name} 没能运行：{e}"
            return res
        found = _found_any(engine, code, text)
        if found is None:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            last = next((ln for ln in lines if "error" in ln.lower()), lines[-1] if lines else f"返回码 {code}")
            hint = "（可能装了别的杀毒软件，Defender 处于关闭状态）" if engine.kind == "defender" else ""
            if "database" in text.lower() and engine.kind == "clamav":
                hint = "（还没有病毒库，先运行 freshclam 下载）"
            res.message = f"{engine.name} 扫描出错{hint}：{last}"
            return res
        res.ok = True
        if found:
            res.threats = parse_output(engine, text)
            if not res.threats:
                res.threats = _pinpoint(engine, staged)
        for sha, dst in staged.items():  # 扫描期间被实时防护删掉的，也算发现
            if not dst.exists() and sha not in res.threats:
                res.threats[sha] = [f"{engine.name} 实时防护已隔离"]
        return res
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _pinpoint(engine: Engine, staged: dict[str, Path], limit: int = 300) -> dict[str, list[str]]:
    """整体扫描报告有威胁但没对上具体文件时，逐个文件再扫一次。"""
    out: dict[str, list[str]] = {}
    for sha, p in list(staged.items())[:limit]:
        if not p.exists():
            continue
        code, text = _run(_command(engine, p))
        if _found_any(engine, code, text):
            names = parse_output(engine, text).get(sha) or [f"{engine.name} 报告有威胁"]
            out[sha] = names
    if not out:
        out["?"] = [f"{engine.name} 报告有威胁，但没能定位到具体文件，请直接用杀毒软件扫描项目的 blobs 文件夹"]
    return out


def scan_project(proj, batch_seq: int | None = None, engine: Engine | None = None) -> ScanResult | None:
    """扫描某一批次新增的文件（batch_seq 为空时扫描全部），结果写进数据库。没有杀毒软件时返回 None。"""
    engine = engine or find_engine()
    if engine is None:
        return None
    with proj.connect() as con:
        if batch_seq is None:
            rows = con.execute("SELECT f.sha256, f.stored_path, MIN(o.batch_seq) AS b, MIN(o.path) AS path FROM files f"
                               " JOIN occurrences o ON o.sha256=f.sha256 WHERE f.kind!='archive' GROUP BY f.sha256").fetchall()
        else:
            rows = con.execute("SELECT f.sha256, f.stored_path, ? AS b, MIN(o.path) AS path FROM files f"
                               " JOIN occurrences o ON o.sha256=f.sha256 AND o.batch_seq=? WHERE f.kind!='archive'"
                               " AND NOT EXISTS (SELECT 1 FROM occurrences p WHERE p.sha256=f.sha256 AND p.batch_seq<?)"
                               " GROUP BY f.sha256", (batch_seq, batch_seq, batch_seq)).fetchall()
    info = {r["sha256"]: r for r in rows}
    res = scan(engine, {sha: proj.root / r["stored_path"] for sha, r in info.items()}, proj.root)
    with proj.connect() as con:
        if res.ok:
            q = ",".join("?" * len(info)) or "''"
            con.execute(f"DELETE FROM risk_findings WHERE source=? AND sha256 IN ({q})", (engine.name, *info))
        for sha, names in res.threats.items():
            r = info.get(sha)
            con.execute("INSERT INTO risk_findings(batch_seq, sha256, path, source, level, kind, detail) VALUES (?,?,?,?,?,?,?)",
                        (r["b"] if r else (batch_seq or 0), sha, r["path"] if r else "（未定位）", engine.name, "high",
                         "antivirus", f"{engine.name} 报告：{'、'.join(names)}"))
        for sha in res.vanished:
            r = info[sha]
            con.execute("INSERT INTO risk_findings(batch_seq, sha256, path, source, level, kind, detail) VALUES (?,?,?,?,?,?,?)",
                        (r["b"], sha, r["path"], engine.name, "high", "vanished",
                         "收图后这个文件不见了或读不出来，多半已被杀毒软件隔离"))
        con.execute("INSERT INTO av_scans(batch_seq, engine, scanned_at, files, threats, ok, message) VALUES (?,?,?,?,?,?,?)",
                    (batch_seq or 0, engine.name, datetime.now().isoformat(timespec="seconds"), res.scanned,
                     len(res.threats) + len(res.vanished), int(res.ok), res.message))
    return res
