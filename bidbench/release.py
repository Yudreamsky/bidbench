"""安装版的更新：从公开的发布仓库下载新的安装包，校验后静默安装，装完自动重新打开。

只有安装版（PyInstaller 打包）走这里；用 git clone 安装的继续用 updater.py 的 git 更新。
发布仓库只放安装包，源码仓库可以保持私有，程序不需要任何密码。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, appconfig
from ._build import BUILD, COMMIT
from .updater import UpdateError

RELEASES_REPO = "Yudreamsky/bidbench-releases"
API = f"https://api.github.com/repos/{RELEASES_REPO}/releases?per_page=30"
TAG = re.compile(r"^b(\d+)$")
TIMEOUT = 30
INSTALLER_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]


def is_installed() -> bool:
    return bool(getattr(sys, "frozen", False)) or os.environ.get("BIDBENCH_RELEASE_MODE") == "1"


def current_build() -> int:
    return int(os.environ.get("BIDBENCH_BUILD_OVERRIDE") or BUILD)


def _open(url: str, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": f"bidbench/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - 固定的 https 地址
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise UpdateError("GitHub 暂时限制了查询次数，过一会儿再试。") from e
        if e.code == 404:
            raise UpdateError(f"没有找到发布仓库 {RELEASES_REPO}，请确认它是公开的。") from e
        raise UpdateError(f"GitHub 返回错误 {e.code}，稍后再试。") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("连不上 GitHub，请检查网络后重试；下载慢可以在设置里填下载加速前缀。") from e


def releases() -> list[dict]:
    """发布仓库里的全部安装包，构建号从新到旧。"""
    with _open(API) as r:
        rows = json.loads(r.read())
    out = []
    for rel in rows:
        m = TAG.match(rel.get("tag_name") or "")
        if not m or rel.get("draft") or rel.get("prerelease"):
            continue
        assets = {a["name"]: a for a in rel.get("assets", [])}
        exe = next((a for n, a in assets.items() if n.lower().endswith(".exe")), None)
        if not exe:
            continue
        sha = assets.get(exe["name"] + ".sha256")
        body = rel.get("body") or ""
        out.append({
            "build": int(m.group(1)), "tag": rel["tag_name"], "title": rel.get("name") or rel["tag_name"],
            "date": (rel.get("published_at") or "")[:10],
            "notes": [ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")],
            "installer": exe["browser_download_url"], "size": exe.get("size"),
            "sha256_url": sha["browser_download_url"] if sha else None,
        })
    out.sort(key=lambda x: -x["build"])
    return out


def version_info() -> dict:
    return {"version": __version__, "commit": COMMIT[:7] or None, "build": current_build(), "mode": "installer",
            "last_update": None, "can_rollback": current_build() > 1}


def check() -> dict:
    info = {"current": version_info(), "commits": [], "deps_changed": False, "mode": "installer"}
    try:
        rels = releases()
    except UpdateError as e:
        return {**info, "status": "error", "message": str(e)}
    cur = current_build()
    newer = [r for r in rels if r["build"] > cur]
    info["previous"] = next((r["tag"] for r in rels if r["build"] < cur), None)
    if not newer:
        return {**info, "status": "up_to_date", "message": "已经是最新版本。", "latest": rels[0]["tag"] if rels else None}
    info["latest"] = newer[0]["tag"]
    info["commits"] = [{"sha": r["tag"], "date": r["date"], "title": t} for r in newer for t in (r["notes"] or [r["title"]])]
    size = f"（约 {newer[0]['size'] / 1048576:.0f} MB）" if newer[0].get("size") else ""
    return {**info, "status": "available", "message": f"有新版本 {newer[0]['tag']}{size}。"}


def _mirror(url: str) -> str:
    prefix = appconfig.load().get("download_prefix", "").strip()
    return prefix.rstrip("/") + "/" + url if prefix else url


def download(rel: dict, dest: Path | None = None, progress=None) -> Path:
    """下载安装包并按发布时的 SHA-256 校验。"""
    dest = Path(dest or tempfile.mkdtemp(prefix="bidbench-update-"))
    target = dest / rel["installer"].rsplit("/", 1)[-1]
    expected = None
    if rel.get("sha256_url"):
        with _open(_mirror(rel["sha256_url"])) as r:
            expected = r.read().decode("ascii", "replace").split()[0].lower()
    h = hashlib.sha256()
    done = 0
    with _open(_mirror(rel["installer"]), timeout=120) as r, open(target, "wb") as f:
        total = int(r.headers.get("Content-Length") or rel.get("size") or 0)
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if expected is None:
        target.unlink(missing_ok=True)
        raise UpdateError("发布里缺少校验文件，为安全起见不安装。")
    if h.hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise UpdateError("下载的安装包校验不通过（可能下载不完整或被篡改），已删除，请重试。")
    return target


def _pick(newer: bool) -> dict:
    rels = releases()
    cur = current_build()
    cands = [r for r in rels if (r["build"] > cur if newer else r["build"] < cur)]
    if not cands:
        raise UpdateError("已经是最新版本。" if newer else "没有更早的版本可以回退。")
    return cands[0]


def apply(progress=None) -> dict:
    rel = _pick(True)
    path = download(rel, progress=progress)
    return {"installer": str(path), "updated_to": rel["tag"], "previous": f"b{current_build()}",
            "commits": [{"title": t} for t in rel["notes"]], "pip_command": None, "pip_done": False}


def rollback(progress=None) -> dict:
    rel = _pick(False)
    path = download(rel, progress=progress)
    return {"installer": str(path), "rolled_back_to": rel["tag"], "from": f"b{current_build()}", "pip_command": None}


def launch_installer(path: str | Path) -> None:
    """启动静默安装。调用方随后退出，安装程序替换文件后会重新打开工作台。"""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen([str(path), *INSTALLER_ARGS], **kwargs)
