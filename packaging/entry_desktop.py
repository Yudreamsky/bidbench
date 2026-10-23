"""打包入口：桌面工作台 bidbench-desktop.exe（无控制台窗口）。"""
import multiprocessing
import sys

multiprocessing.freeze_support()  # 同时读 DWG 开的读图进程也是这个程序：是读图进程就在这里读图，不再开一个工作台

from bidbench.desktop import main  # noqa: E402

sys.exit(main())
