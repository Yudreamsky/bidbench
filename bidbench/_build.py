"""安装包构建信息。开发时保持默认值；打安装包时由 packaging/write_build_info.py 改写。"""

BUILD = 0       # 安装包构建号（GitHub Actions 的运行编号），0 表示不是安装版
COMMIT = ""     # 构建时的提交
