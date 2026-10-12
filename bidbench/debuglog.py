"""排错日志（给开发用）：程序内部做了什么、出错时的完整错误信息。

写在本机用户目录的 logs 文件夹（Windows 为 %APPDATA%\\bidbench\\logs），每天一个文件，保留最近 14 天。
只记操作类型、用时、外部工具（解压、杀毒、更新）的命令和返回结果、出错时的错误堆栈；
不记图纸内容、识别出的文字、API 密钥和压缩包密码。出问题时把这个文件夹里的文件发给开发者即可。

用法：各模块 logging.getLogger(__name__) 照常写；进程入口（命令行、桌面窗口）调用一次 setup()。
没有调用 setup() 时（被当作库使用、跑测试）什么都不写。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

from . import __version__, appconfig

KEEP_DAYS = 14
_TAG = "_bidbench_debuglog"
_hooked = False
log = logging.getLogger("bidbench")


def folder() -> Path:
    d = appconfig.config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cleanup(d: Path) -> None:
    oldest = date.today() - timedelta(days=KEEP_DAYS)
    for p in d.glob("bidbench-*.log"):
        m = re.match(r"bidbench-(\d{4})-(\d{2})-(\d{2})\.log$", p.name)
        try:
            if m and date(*map(int, m.groups())) < oldest:
                p.unlink()
        except (OSError, ValueError):
            pass


def setup(name: str, hooks: bool = False) -> Path | None:
    """开始写排错日志。name 是进程的用途（desktop、receive、mcp 等），写在每一行里，几个进程同时写也分得清。
    重复调用会替换之前的设置。写不了（目录没有权限等）时返回 None，不影响程序运行。"""
    close()
    try:
        d = folder()
        _cleanup(d)
        path = d / f"bidbench-{date.today():%Y-%m-%d}.log"
        # 不在运行中轮换文件：Windows 上几个进程同时打开同一个文件时改名会失败。按天分文件，启动时清理旧的。
        h = logging.FileHandler(path, encoding="utf-8", delay=True)
    except OSError:
        return None
    setattr(h, _TAG, True)
    h.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{name} %(process)d] %(name)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.DEBUG if os.environ.get("BIDBENCH_DEBUG") else logging.INFO)
    if hooks:
        _install_hooks()
    from ._build import BUILD, COMMIT
    log.info("启动：bidbench %s%s，Python %s，%s%s", __version__, f"（构建 b{BUILD}，{COMMIT[:7]}）" if BUILD else "",
             platform.python_version(), platform.platform(), "，安装版" if getattr(sys, "frozen", False) else "")
    return path


def close() -> None:
    for h in [h for h in log.handlers if getattr(h, _TAG, False)]:
        log.removeHandler(h)
        h.close()


def _install_hooks() -> None:
    """桌面窗口：没被接住的错误（包括后台线程里的）也写进排错日志。"""
    global _hooked
    if _hooked:
        return
    _hooked = True
    prev, prev_thread = sys.excepthook, threading.excepthook

    def hook(t, v, tb):
        if not issubclass(t, KeyboardInterrupt):
            log.critical("没被接住的错误", exc_info=(t, v, tb))
        prev(t, v, tb)

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            log.critical("线程 %s 里没被接住的错误", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        prev_thread(args)

    sys.excepthook, threading.excepthook = hook, thread_hook


_SECRET_ARG = re.compile(r"^(-p|--password=)(.+)$")


def safe_command(cmd) -> str:
    """把命令写进日志前去掉密码：7-Zip / UnRAR 的 -p密码，bidbench 的 --password 密码。"""
    out: list[str] = []
    for a in map(str, cmd):
        m = _SECRET_ARG.match(a)
        if out and out[-1] == "--password":
            a = "***"
        elif m and m.group(2) != "-":  # -p- 是“不用密码”
            a = f"{m.group(1)}***"
        out.append(a)
    return " ".join(f'"{a}"' if " " in a else a for a in out)


def tail(text: str, n: int = 600) -> str:
    """外部工具的输出只留最后一段。"""
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]
