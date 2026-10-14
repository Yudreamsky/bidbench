# PyInstaller 打包配置：一个文件夹里放两个程序，共用同一套库。
#   bidbench-desktop.exe  桌面工作台（无控制台窗口）
#   bidbench.exe          命令行，AI 助手（MCP）也用它
#   _internal/libredwg/   LibreDWG 的 dwgread.exe（读 DWG，GPLv3，附许可证），先运行 packaging/fetch_libredwg.py
# 用法（仓库根目录）：pyinstaller packaging/bidbench.spec --noconfirm --clean
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import os

HERE = SPECPATH  # noqa: F821 - PyInstaller 提供
ROOT = os.path.dirname(HERE)
datas = collect_data_files("bidbench")
LIBREDWG = os.path.join(HERE, "build", "libredwg")  # 读 DWG 用的 LibreDWG（packaging/fetch_libredwg.py 取来），单独的程序
if os.path.isdir(LIBREDWG):
    datas.append((LIBREDWG, "libredwg"))
hidden = (collect_submodules("bidbench")
          + collect_submodules("anthropic", filter=lambda m: "httpx_aiohttp" not in m)
          + collect_submodules("mcp", filter=lambda m: not m.startswith("mcp.cli"))
          + collect_submodules("py7zr"))
excludes = ["tkinter", "matplotlib", "numpy", "pandas", "PIL", "lxml", "gi", "IPython", "pytest"]  # 用不到的大库


def analysis(script):
    return Analysis([f"{HERE}/{script}"], pathex=[ROOT], datas=datas, hiddenimports=hidden, excludes=excludes)  # noqa: F821


a_desktop = analysis("entry_desktop.py")
a_cli = analysis("entry_cli.py")
icon = f"{HERE}/build/bidbench.ico"

utf8 = [("X utf8_mode=1", None, "OPTION")]  # 打包后的程序不读 PYTHONUTF8 环境变量，这里直接打开 UTF-8 模式

exe_desktop = EXE(PYZ(a_desktop.pure), a_desktop.scripts, utf8, exclude_binaries=True,  # noqa: F821
                  name="bidbench-desktop", console=False, icon=icon)
exe_cli = EXE(PYZ(a_cli.pure), a_cli.scripts, utf8, exclude_binaries=True,  # noqa: F821
              name="bidbench", console=True, icon=icon)

COLLECT(exe_desktop, a_desktop.binaries, a_desktop.datas,  # noqa: F821
        exe_cli, a_cli.binaries, a_cli.datas, name="bidbench")
