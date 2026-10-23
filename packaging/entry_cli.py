"""打包入口：命令行 bidbench.exe（带控制台窗口，命令行和 AI 助手 MCP 用）。"""
import multiprocessing

multiprocessing.freeze_support()  # 同时读 DWG 开的读图进程也是这个程序：是读图进程就在这里读图，不往下走

from bidbench.cli import main  # noqa: E402

main()
