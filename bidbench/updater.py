"""一键更新：从 GitHub 拉取最新代码（只做快进），需要时补装依赖，然后重启。

前提是用 git clone + pip install -e 安装的。跟随 main 分支：每个 PR 合并前都通过 CI。
安全原则：
- 本地改过代码、不在 main 分支、与远程分叉时停下，不覆盖、不自动合并
- 记录更新前的版本，可以回退
- 沿用本机 git 已有的登录凭据，不弹出输入框、不保存任何密码
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__

BRANCH = "main"
REMOTE = "origin"
FETCH_TIMEOUT = 90
EXTRAS = {"claude": "anthropic", "mcp": "mcp", "archives": "py7zr"}


class UpdateError(Exception):
    """无法检查或更新。消息直接给用户看。"""


def repo_root() -> Path | None:
    root = Path(__file__).resolve().parents[1]
    return root if (root / ".git").exists() else None


def _git(root: Path, *args: str, timeout: int = 30, check: bool = True) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C")
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise UpdateError("找不到 git 命令，请先安装 Git") from e
    except subprocess.TimeoutExpired as e:
        raise UpdateError("连接 GitHub 超时，请检查网络后重试") from e
    if check and r.returncode != 0:
        raise UpdateError(_explain(r.stderr.strip() or r.stdout.strip()))
    return r.stdout.strip()


def _git_ok(root: Path, *args: str) -> bool:
    """只关心 git 命令是否成功（例如 merge-base --is-ancestor）。"""
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _explain(err: str) -> str:
    low = err.lower()
    if any(w in low for w in ("authentication", "could not read username", "permission denied", "403", "terminal prompts disabled")):
        return "GitHub 登录失效或没有仓库权限。请在命令行里运行一次 git fetch，按提示登录后再重试。"
    if any(w in low for w in ("could not resolve host", "unable to access", "timed out", "connection")):
        return "连不上 GitHub，请检查网络后重试。"
    return f"git 出错：{err.splitlines()[-1] if err else '未知原因'}"


def _record_path(root: Path) -> Path:
    return root / ".git" / "bidbench_update.json"


def _installed_extras() -> list[str]:
    return [extra for extra, module in EXTRAS.items() if importlib.util.find_spec(module) is not None]


def version_info(root: Path | None = None) -> dict:
    root = root or repo_root()
    sha = None
    if root:
        try:
            sha = _git(root, "rev-parse", "--short", "HEAD")
        except UpdateError:
            sha = None
    rec = {}
    if root and _record_path(root).exists():
        try:
            rec = json.loads(_record_path(root).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rec = {}
    return {"version": __version__, "commit": sha, "last_update": rec.get("at"), "can_rollback": bool(rec.get("previous"))}


def check(root: Path | None = None, fetch: bool = True) -> dict:
    """检查有没有新版本。返回 status：up_to_date / available / dirty / diverged / not_on_main / not_git / error。"""
    root = root or repo_root()
    info = {"current": version_info(root), "commits": [], "deps_changed": False}
    if root is None:
        return {**info, "status": "not_git",
                "message": "当前不是用 git clone 安装的，无法自动更新。请重新用 git clone 安装，或手动下载新版本。"}
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != BRANCH:
            return {**info, "status": "not_on_main",
                    "message": f"当前在 {branch} 分支上，自动更新只跟随 {BRANCH}。切回 {BRANCH}（git checkout {BRANCH}）后再更新。"}
        if fetch:
            _git(root, "fetch", "--quiet", REMOTE, BRANCH, timeout=FETCH_TIMEOUT)
        target = f"{REMOTE}/{BRANCH}"
        ahead, behind = (int(x) for x in _git(root, "rev-list", "--left-right", "--count", f"HEAD...{target}").split())
        log = _git(root, "log", "--format=%h%x09%cs%x09%s", f"HEAD..{target}")
        info["commits"] = [dict(zip(("sha", "date", "title"), ln.split("\t", 2), strict=False)) for ln in log.splitlines() if ln]
        info["deps_changed"] = bool(_git(root, "diff", "--name-only", "HEAD", target, "--", "pyproject.toml"))
        info["latest"] = _git(root, "rev-parse", "--short", target)
        dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    except UpdateError as e:
        return {**info, "status": "error", "message": str(e)}
    if ahead and behind:
        return {**info, "status": "diverged",
                "message": "本地有远程没有的提交，和远程已经分叉。为免丢失你的改动，不自动更新，请手动处理。"}
    if dirty:
        files = "、".join(ln.split(maxsplit=1)[-1] for ln in dirty.splitlines()[:5])
        return {**info, "status": "dirty",
                "message": f"本地改过代码（{files}），为免覆盖不自动更新。用 git stash 暂存或提交后再更新。"}
    if not behind:
        return {**info, "status": "up_to_date", "message": "已经是最新版本。"}
    return {**info, "status": "available", "message": f"有 {behind} 项更新。"}


def _pip_install(root: Path, extras: list[str]) -> list[str]:
    target = f"{root}[{','.join(extras)}]" if extras else str(root)
    return [sys.executable, "-m", "pip", "install", "--quiet", "-e", target]


def apply(root: Path | None = None, run_pip=None) -> dict:
    """快进更新。返回 {updated_to, previous, deps_changed, pip_command}；pip 已执行时 pip_done 为真。"""
    root = root or repo_root()
    st = check(root)
    if st["status"] != "available":
        raise UpdateError(st["message"])
    previous = _git(root, "rev-parse", "HEAD")
    _git(root, "merge", "--ff-only", "--quiet", f"{REMOTE}/{BRANCH}")
    _record_path(root).write_text(json.dumps({"previous": previous, "at": datetime.now().isoformat(timespec="seconds"),
                                              "to": _git(root, "rev-parse", "HEAD")}), encoding="utf-8")
    cmd = _pip_install(root, _installed_extras()) if st["deps_changed"] else None
    done = False
    if cmd and run_pip is not None:
        run_pip(cmd)
        done = True
    return {"updated_to": _git(root, "rev-parse", "--short", "HEAD"), "previous": previous[:7],
            "commits": st["commits"], "deps_changed": st["deps_changed"], "pip_command": cmd, "pip_done": done}


def rollback(root: Path | None = None, run_pip=None) -> dict:
    """回到上一次更新之前的版本（保留本地未提交的改动；有冲突时拒绝）。"""
    root = root or repo_root()
    if root is None:
        raise UpdateError("当前不是用 git clone 安装的，无法回退。")
    rec_path = _record_path(root)
    if not rec_path.exists():
        raise UpdateError("没有更新记录，无从回退。")
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    prev = rec.get("previous")
    if not prev:
        raise UpdateError("没有可以回退的版本。")
    head = _git(root, "rev-parse", "HEAD")
    if not _git_ok(root, "merge-base", "--is-ancestor", prev, head):
        raise UpdateError("当前版本不是从记录的版本更新来的，为安全起见不自动回退。")
    deps = bool(_git(root, "diff", "--name-only", prev, head, "--", "pyproject.toml"))
    _git(root, "reset", "--keep", prev)
    rec_path.unlink()
    cmd = _pip_install(root, _installed_extras()) if deps else None
    if cmd and run_pip is not None:
        run_pip(cmd)
    return {"rolled_back_to": prev[:7], "from": head[:7], "deps_changed": deps, "pip_command": cmd,
            "pip_done": bool(cmd and run_pip is not None)}


# ---------------- 重启与延后安装 ----------------

def spawn_helper(wait_pid: int, pip_command: list[str] | None, restart: list[str] | None, log: Path) -> None:
    """启动一个独立的小进程：等当前进程退出后再补装依赖、再重启。
    这样 Windows 上不会因为 bidbench.exe 正在运行而装不上依赖。"""
    args = [sys.executable, "-m", "bidbench.updater", "--wait-pid", str(wait_pid), "--log", str(log)]
    if pip_command:
        args += ["--pip", json.dumps(pip_command)]
    if restart:
        args += ["--restart", json.dumps(restart)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def restart_command() -> list[str]:
    """用 python -m bidbench 重启，避开 Windows 上被锁住的 bidbench.exe。"""
    return [sys.executable, "-m", "bidbench", *sys.argv[1:]]


def _alive(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _helper_main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-pid", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--pip")
    ap.add_argument("--restart")
    a = ap.parse_args(argv)
    log = open(a.log, "a", encoding="utf-8")
    for _ in range(300):  # 最多等 60 秒
        if not _alive(a.wait_pid):
            break
        time.sleep(0.2)
    time.sleep(0.5)
    if a.pip:
        log.write(f"{datetime.now():%F %T} 补装依赖：{' '.join(json.loads(a.pip))}\n")
        log.flush()
        r = subprocess.run(json.loads(a.pip), stdout=log, stderr=log)
        log.write(f"{datetime.now():%F %T} pip 退出码 {r.returncode}\n")
    if a.restart:
        log.write(f"{datetime.now():%F %T} 重启：{' '.join(json.loads(a.restart))}\n")
        log.flush()
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(json.loads(a.restart), **kwargs)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper_main(sys.argv[1:]))
