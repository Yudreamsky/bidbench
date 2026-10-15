"""给 Revit MCP 翻模用的 DXF（cad_file）：现行版的 CAD 文件、同文件的其他图、外部参照，DWG 用 dwg2dxf 转。"""

import os
import stat
from pathlib import Path

import pytest

from bidbench import cadfile, ops
from bidbench.assistant import Toolkit, ToolError
from bidbench.store import Project

import make_sample
from conftest import _recognize


@pytest.fixture
def cad_project(tmp_path):
    root, date = make_sample.build_cad(tmp_path / "in")
    proj = Project.init(tmp_path / "p", "CAD 项目")
    ops.receive(proj, root, date, av=False, log=lambda s: None)
    _recognize(proj)
    return proj


def test_cad_file_for_sheets(cad_project):
    kit = Toolkit(cad_project)
    r = kit.cad_file("建施-04")  # DXF 是现行版
    assert r["rev"] == "B" and "warning" not in r and r["where"] == "模型空间"
    dxf = Path(r["dxf"])
    assert dxf.parent == cadfile.out_dir(cad_project) and dxf.name.startswith("建施-04") and dxf.read_bytes()
    assert r["xrefs"] == [{"name": "标准层柱网", "dxf": None, "note": "没收到这个外部参照文件，轴网、底图可能在里面，列进答疑"}]
    assert kit.cad_file("建施-04")["dxf"] == r["dxf"]  # 第二次直接用
    r3 = kit.cad_file("建施-03")  # 现行页是 PDF，配成一份的 DXF 同为 B 版
    assert r3["rev"] == "B" and r3["dxf"].endswith(".dxf") and "warning" not in r3
    assert kit.cad_file("建施-13")["same_file_sheets"] == ["建施-14"]
    assert kit.cad_file("PRJ1-A-701")["where"] == "布局“A-701”"
    with pytest.raises(ToolError, match="没有收到 DWG 或 DXF|没有图号"):
        kit.cad_file("建施-15")  # 缺图


def _fake(tmp_path, body: str) -> Path:
    tool = tmp_path / "dwg2dxf"
    tool.write_text("#!/bin/sh\n" + body)
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return tool


@pytest.mark.skipif(os.name == "nt", reason="假的 dwg2dxf 是 shell 脚本；Windows 上打包自检用真的转")
def test_dwg_converted_with_dwg2dxf(tmp_path, monkeypatch):
    proj = Project.init(tmp_path / "p", "x")
    src = tmp_path / "blob.dwg"
    src.write_bytes(b"AC1015")
    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(_fake(tmp_path, 'echo "0\\nEOF" > "$3"\n')))  # dwg2dxf -y -o 输出 输入
    out = cadfile.to_dxf(proj, src, "ab" * 32, "图纸/结施-04 三层梁.dwg")
    assert out.name == "结施-04 三层梁-abababab.dxf" and out.read_text().startswith("0")

    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(_fake(tmp_path, "exit 1\n")))
    with pytest.raises(cadfile.CadError, match="转不出"):
        cadfile.to_dxf(proj, src, "cd" * 32, "坏.dwg")
    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(_fake(tmp_path, 'echo x > "$3"; sleep 5\n')))
    monkeypatch.setattr(cadfile.dwg, "timeout_for", lambda size: 0.5)
    with pytest.raises(cadfile.CadError, match="太久"):
        cadfile.to_dxf(proj, src, "ef" * 32, "慢.dwg")
    assert sorted(p.name for p in cadfile.out_dir(proj).iterdir()) == ["结施-04 三层梁-abababab.dxf"]  # 没写完的删掉

    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(tmp_path / "没有"))
    with pytest.raises(cadfile.CadError, match="另存为 DXF"):
        cadfile.to_dxf(proj, src, "12" * 32, "a.dwg")
