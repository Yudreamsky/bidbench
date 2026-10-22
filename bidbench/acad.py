"""本机装了 AutoCAD 时，用它自带的命令行版 accoreconsole 把 DWG 转成 DXF（docs/图纸字段.md 9.7 第 5 条）。

LibreDWG 读不出、读不全的 DWG（版本太新、自定义对象多）用这个兜底。DWG 先复制到临时文件夹、换成英文名
（accoreconsole 对中文路径不可靠），转完、失败都删掉临时文件夹。没装 AutoCAD 的，find_tool() 返回 None。
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import debuglog, dwg, proc

log = logging.getLogger(__name__)


class AcadError(Exception):
    """转不了：给人看的原因。"""


def find_tool() -> str | None:
    """accoreconsole 在哪：环境变量 BIDBENCH_ACCORECONSOLE > 装的 AutoCAD 里最新的一个 > 系统路径里的。"""
    env = os.environ.get("BIDBENCH_ACCORECONSOLE")
    if env:
        return env if Path(env).exists() else None
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("ProgramW6432", "ProgramFiles")]
        found = sorted({p for r in roots if r for p in glob.glob(os.path.join(r, "Autodesk", "AutoCAD*", "accoreconsole.exe"))})
        if found:
            return found[-1]  # AutoCAD 2025 排在 2024 后面
    return shutil.which("accoreconsole")


def dxfout(src: Path, out: Path, tool: str | None = None) -> Path:
    """把 src（DWG）用 AutoCAD 转成 out（DXF，2018 格式）。转不出时 out 不存在，抛 AcadError。"""
    tool = tool or find_tool()
    if not tool:
        raise AcadError("本机没有找到 AutoCAD（accoreconsole），转不了")
    work = Path(tempfile.mkdtemp(prefix="bidbench-acad-"))
    try:
        dwg_in = work / "in.dwg"
        dxf_out = work / "out.dxf"
        shutil.copyfile(src, dwg_in)
        script = work / "dxfout.scr"
        # FILEDIA 0：不弹文件对话框；DXFOUT 文件名、版本 2018、精度 16 位
        script.write_text(f'FILEDIA 0\n(command "_.DXFOUT" "{dxf_out.as_posix()}" "_V" "2018" "16")\n', encoding="utf-8")
        cmd = [tool, "/i", str(dwg_in), "/s", str(script), "/l", "en-US"]
        log.debug("AutoCAD 转 DXF：%s", debuglog.safe_command(cmd))
        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=dwg.timeout_for(src.stat().st_size),
                               cwd=work, **proc.NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise AcadError("AutoCAD 转了太久还没转完，跳过") from None
        except OSError as e:
            raise AcadError(f"AutoCAD 启动不了：{e}") from None
        if not dxf_out.exists() or not dwg.dxf_complete(dxf_out):
            log.debug("accoreconsole 退出码 %s，没有转出完整的 DXF", p.returncode)
            raise AcadError("AutoCAD 也转不出这个 DWG")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".part.dxf")
        try:
            shutil.move(str(dxf_out), tmp)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def read(src: Path, tool: str | None = None) -> dwg.Drawing:
    """用 AutoCAD 转成 DXF 再读；DXF 放在临时文件夹，读完就删。"""
    work = Path(tempfile.mkdtemp(prefix="bidbench-acad-read-"))
    try:
        d = dwg.read_dxf(dxfout(src, work / "out.dxf", tool))
        d.reader = "AutoCAD"
        return d
    finally:
        shutil.rmtree(work, ignore_errors=True)
