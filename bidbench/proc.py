"""调用外部命令行程序（解压工具、杀毒软件、Codex）的公共设置。"""

from __future__ import annotations

import os
import subprocess

# 桌面版是没有控制台的窗口程序：不加这个参数，Windows 会给每个被调用的命令行工具弹出一个黑色窗口
NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}  # type: ignore[attr-defined]
