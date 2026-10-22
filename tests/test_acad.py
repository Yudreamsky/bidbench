"""本机装了 AutoCAD 时，LibreDWG 读不出、转不出的 DWG 用 accoreconsole 的 DXFOUT 转（docs/图纸字段.md 9.7 第 5 条）。"""

import os
import stat
import tempfile
from pathlib import Path

import pytest

from bidbench import acad, cadfile, dwg
from bidbench.store import Project

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假的 accoreconsole 是 shell 脚本")

DXF = "0\\nSECTION\\n2\\nENTITIES\\n0\\nTEXT\\n8\\n0\\n10\\n0\\n20\\n0\\n40\\n2.5\\n1\\n建施-01\\n0\\nENDSEC\\n0\\nEOF\\n"


def _fake(tmp_path, works: bool = True) -> Path:
    """假的 accoreconsole：/i 输入 /s 脚本；从脚本里找 DXFOUT 的文件名写一个 DXF，并记下收到的输入文件名。"""
    tool = tmp_path / "accoreconsole"
    body = (f'out=$(sed -n \'s/.*"_.DXFOUT" "\\([^"]*\\)".*/\\1/p\' "$4")\nbasename "$2" > "{tmp_path}/input.txt"\n'
            + (f'printf "{DXF}" > "$out"\n' if works else "exit 3\n"))
    tool.write_text("#!/bin/sh\n" + body)
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return tool


def _leftovers() -> list[str]:
    return [p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("bidbench-acad")]


def test_dwg_read_by_autocad_when_libredwg_cannot(tmp_path, monkeypatch):
    src = tmp_path / "建施-01 平面.dwg"
    src.write_bytes(b"AC1032")
    monkeypatch.setenv("BIDBENCH_DWGREAD", str(tmp_path / "没有"))
    with pytest.raises(dwg.ReadError, match="LibreDWG"):
        dwg.read_dwg(src)  # 没装 AutoCAD：照旧读不出
    monkeypatch.setenv("BIDBENCH_ACCORECONSOLE", str(_fake(tmp_path)))
    d = dwg.read_dwg(src)
    assert d.reader == "AutoCAD" and any(t.text == "建施-01" for t in d.texts)
    assert (tmp_path / "input.txt").read_text().strip() == "in.dwg"  # 复制成英文名再转
    assert _leftovers() == []

    monkeypatch.setenv("BIDBENCH_ACCORECONSOLE", str(_fake(tmp_path, works=False)))
    with pytest.raises(dwg.ReadError, match="AutoCAD 也转不出"):
        dwg.read_dwg(src)
    assert _leftovers() == []  # 失败也删掉临时文件夹


def test_cad_file_falls_back_to_autocad(tmp_path, monkeypatch):
    proj = Project.init(tmp_path / "p", "x")
    src = tmp_path / "blob.dwg"
    src.write_bytes(b"AC1032")
    lib = tmp_path / "dwg2dxf"
    lib.write_text('#!/bin/sh\nprintf "0\\nSECTION\\n" > "$3"\n')  # 只转出一半
    lib.chmod(lib.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(lib))
    half = cadfile.to_dxf(proj, src, "ab" * 32, "建施-01.dwg")
    assert not dwg.dxf_complete(half)  # 没装 AutoCAD：留着转了一半的
    monkeypatch.setenv("BIDBENCH_ACCORECONSOLE", str(_fake(tmp_path)))
    again = cadfile.to_dxf(proj, src, "ab" * 32, "建施-01.dwg")
    assert again == half and dwg.dxf_complete(again) and "建施-01" in again.read_text()  # 装了以后再要，就用 AutoCAD 转全
    lib.write_text("#!/bin/sh\nexit 1\n")
    other = cadfile.to_dxf(proj, src, "cd" * 32, "建施-02.dwg")
    assert dwg.dxf_complete(other)
    assert not list(cadfile.out_dir(proj).glob("*.part.dxf")) and _leftovers() == []
    assert acad.find_tool() == str(tmp_path / "accoreconsole")
