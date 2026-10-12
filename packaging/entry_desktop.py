"""打包入口：桌面工作台 bidbench-desktop.exe（无控制台窗口）。"""
import sys

from bidbench.desktop import main

sys.exit(main())
