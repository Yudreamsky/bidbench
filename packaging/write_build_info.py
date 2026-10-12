"""打安装包前写入构建号和提交：python packaging/write_build_info.py <构建号> <提交>。"""
import sys
from pathlib import Path

build, commit = int(sys.argv[1]), sys.argv[2]
target = Path(__file__).resolve().parents[1] / "bidbench" / "_build.py"
target.write_text(
    '"""安装包构建信息（由 packaging/write_build_info.py 生成）。"""\n\n'
    f"BUILD = {build}\nCOMMIT = {commit!r}\n", encoding="utf-8")
print(f"构建 b{build}（{commit[:7]}）")
