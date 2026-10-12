"""调用系统解压工具解 rar / 7z，不额外安装 Python 依赖。

按顺序尝试已有的工具：
- bsdtar（Windows 10 及以上、macOS 自带的 tar 就是它；Linux 装 libarchive-tools）：
  支持不加密的 rar4 / rar5 / 7z，拒绝越界路径，最安全，优先使用
- unrar（WinRAR 自带的 UnRAR）：支持加密、各版本 rar
- 7-Zip（7z / 7za / 7zz）：支持 7z、加密；是否支持 rar 取决于发行版

任何工具返回失败都整体作废，不使用解了一半的文件（加密包会解出乱码）。
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 3600


@dataclass
class Outcome:
    ok: bool
    tool: str
    encrypted: bool = False
    message: str = ""


def _env() -> dict:
    env = dict(os.environ)
    if os.name != "nt":
        # 非 UTF-8 环境下 bsdtar 解不出中文等非 ASCII 文件名
        env["LC_ALL"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=_env(), timeout=TIMEOUT, stdin=subprocess.DEVNULL)


def _first(*candidates: str) -> str | None:
    for c in candidates:
        p = shutil.which(c) if not os.path.isabs(c) else (c if os.path.exists(c) else None)
        if p:
            return p
    return None


@functools.lru_cache(maxsize=None)
def find_unrar() -> str | None:
    return _first("unrar", "UnRAR", r"C:\Program Files\WinRAR\UnRAR.exe", r"C:\Program Files (x86)\WinRAR\UnRAR.exe")


@functools.lru_cache(maxsize=None)
def find_7z() -> str | None:
    return _first("7zz", "7z", "7za", r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe")


@functools.lru_cache(maxsize=None)
def find_bsdtar() -> str | None:
    p = shutil.which("bsdtar")
    if p:
        return p
    tar = shutil.which("tar")
    if not tar:
        return None
    try:
        out = subprocess.run([tar, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return tar if "bsdtar" in out or "libarchive" in out else None


def no_tool_hint(kind: str) -> str:
    return (f"{kind} 压缩包需要解压工具：Windows 10 及以上、macOS 自带的 tar 即可；"
            "也可以安装 7-Zip 或 WinRAR；Linux 请安装 libarchive-tools（bsdtar）、p7zip 或 unrar")


def _short(text: str) -> str:
    """工具输出的摘要：优先保留含 error 的行（真正的原因），再补最后一行。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    key = [ln for ln in lines if "error" in ln.lower() and "errors:" not in ln.lower()]
    picked = list(dict.fromkeys(key[:2] + lines[-1:]))
    return "；".join(picked)[:300]


def _looks_encrypted(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ("encrypt", "password", "passphrase", "wrong password", "需要密码", "密码"))


def _clean(dest: Path) -> None:
    for p in list(dest.iterdir()):
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def inspect(archive: Path) -> tuple[int | None, bool]:
    """用 bsdtar 预先列出内容：解压后总大小（防压缩炸弹）、是否含符号链接。拿不到时为 (None, False)。"""
    tool = find_bsdtar()
    if not tool:
        return None, False
    try:
        r = _run([tool, "-tvf", str(archive)])
    except (OSError, subprocess.SubprocessError):
        return None, False
    total, has_link = 0, False
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if not parts:
            continue
        has_link = has_link or parts[0].startswith("l")
        if len(parts) > 4 and parts[4].isdigit():
            total += int(parts[4])
    return total, has_link


def listed_size(archive: Path) -> int | None:
    return inspect(archive)[0]


def extract(archive: Path, dest: Path, kind: str, passwords: list[str]) -> Outcome:
    """依次用可用的工具解到 dest，成功即停。全部失败时 dest 为空。

    顺序：bsdtar（拒绝越界路径和穿过符号链接写文件，最安全）→ unrar → 7-Zip（用于加密包等 bsdtar 不支持的情况）。
    压缩包含符号链接时，bsdtar 失败后不再换其他工具，避免老版本工具被链接带到压缩包以外。
    """
    _size, has_link = inspect(archive)
    attempts: list[tuple[str, list[list[str]]]] = []
    pw = passwords or []
    if find_bsdtar():
        attempts.append(("bsdtar", [[find_bsdtar(), "-x", "-f", str(archive), "-C", str(dest)]]))
    if not (has_link and find_bsdtar()):
        if kind == "rar" and find_unrar():
            attempts.append(("unrar", [[find_unrar(), "x", "-y", "-idq", f"-p{p}" if p else "-p-", str(archive),
                                        str(dest) + os.sep] for p in ([None] + pw)]))
        if find_7z():
            attempts.append(("7-Zip", [[find_7z(), "x", "-y", "-bd", f"-o{dest}", f"-p{p or ''}", str(archive)]
                                       for p in ([None] + pw)]))
    if not attempts:
        return Outcome(False, "", message=no_tool_hint(kind))
    last = Outcome(False, "")
    encrypted = False
    for tool, cmds in attempts:
        for cmd in cmds:
            _clean(dest)
            try:
                r = _run(cmd)
            except (OSError, subprocess.SubprocessError) as e:
                last = Outcome(False, tool, message=f"{tool} 无法运行：{e}")
                break
            if r.returncode == 0:
                return Outcome(True, tool)
            msg = _short(r.stderr + "\n" + r.stdout)
            encrypted = encrypted or _looks_encrypted(msg)
            last = Outcome(False, tool, encrypted, msg)
    _clean(dest)
    last.encrypted = encrypted
    if has_link:
        last.message = "压缩包含有符号链接，出于安全考虑只用 bsdtar 尝试。" + last.message
    return last
