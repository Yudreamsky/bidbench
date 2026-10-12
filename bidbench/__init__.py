"""投标图纸工作台：面向 AI agent 的投标图纸数据底座。"""

import logging

__version__ = "0.2.0"

logging.getLogger("bidbench").addHandler(logging.NullHandler())  # 没有调用 debuglog.setup() 时不输出任何日志
