"""给 Revit MCP 翻模用的 DXF（cad_file）：现行版的 CAD 文件、同文件的其他图、外部参照，DWG 用 dwg2dxf 转。"""

import os
import stat
from pathlib import Path

import pytest

from bidbench import cadfile, dwg, ops
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
    assert r["region"] == [[0, 0, 420, 297]] and "region_note" not in r  # 图框以外的东西不读
    r13, r14 = kit.cad_file("建施-13"), kit.cad_file("建施-14")
    assert r13["same_file_sheets"] == ["建施-14"] and r13["dxf"] == r14["dxf"]
    assert r13["region"] == [[0, 0, 420, 297]] and r14["region"] == [[500, 0, 920, 297]]  # 各取自己的图框
    a701 = kit.cad_file("PRJ1-A-701")
    assert a701["where"] == "布局“A-701”" and a701["region"] == [[0, 0, 30000, 20000]]  # 视口看到的模型空间
    assert kit.cad_file("PRJ1-A-702")["region"] == [[35000, -5000, 55000, 25000]]  # 视口转了 90°
    assert kit.cad_file("A-150")["region_note"].startswith("这张图画在布局上")  # Revit MCP 读不到布局上的图
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
    assert sorted(p.name for p in cadfile.out_dir(proj).iterdir()) == ["结施-04 三层梁-abababab.blocks", "结施-04 三层梁-abababab.dxf"]  # 没写完的删掉

    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(tmp_path / "没有"))
    with pytest.raises(cadfile.CadError, match="另存为 DXF"):
        cadfile.to_dxf(proj, src, "12" * 32, "a.dwg")


def test_twisted_viewport_window():
    """实测的图：布局视口转了 90°，目标点在模型空间远处（看到的是 x 102023~127793、y 5635~66313 的轴网）。"""
    v = dwg.Viewport("S-2001", 0, 0, 72500 / 100, 58400 / 100, -22107.4, -62204.6, 58400, 90, 182999.2, 13641.8)
    x0, y0, x1, y1 = v.window()
    assert [round(x0), round(y0), round(x1), round(y1)] == [91595, -501, 149995, 71999]
    px, py = v.to_paper(120794.6, 35749.2)  # 范围中心落在视口中心
    assert abs(px) < 1e-6 and abs(py) < 1e-6


def test_title_tag_is_not_a_frame():
    """模型空间里的“图签块”只是图名旁边的小标签（JUDUL 层），图上的字铺得远比它大：不当图框。"""
    texts = [dwg.Text("KOLOM K1", x, y, dwg.MODEL) for x, y in ((0, 0), (60000, 0), (0, 40000), (60000, 40000))]
    tag = dwg.Sheet(dwg.MODEL, {"drawing_no": "S-3002C"}, {}, [("NO", "S-3002C")], texts, (30000, -3000, 33000, -2000))
    assert "region" not in cadfile._region(dwg.Drawing(), tag) and "小标签" in cadfile._region(dwg.Drawing(), tag)["note"]
    frame = dwg.Sheet(dwg.MODEL, {"drawing_no": "S-1"}, {}, [("NO", "S-1")], texts, (-1000, -5000, 61000, 41000))
    assert cadfile._region(dwg.Drawing(), frame) == {"region": [[-1000, -5000, 61000, 41000]]}


def _insert(name: str, handle: str) -> list[str]:
    return ["0", "INSERT", "5", handle, "8", "TNOT", "2", name, "10", "0", "20", "0", "30", "0"]


def _anon_dxf(path: Path) -> Path:
    """dwg2dxf 转出的样子：块记录表里写着 *U4、*U6（句柄 A1、A2），块定义也在，块参照却写成“*U”。"""
    body = ["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "BLOCK_RECORD",
            "0", "BLOCK_RECORD", "5", "A1", "2", "*U4", "0", "BLOCK_RECORD", "5", "A2", "2", "*U6",
            "0", "BLOCK_RECORD", "5", "A3", "2", "*U9", "0", "ENDTAB", "0", "ENDSEC",
            "0", "SECTION", "2", "BLOCKS", "0", "BLOCK", "330", "A1", "2", "*U4", "70", "1", "0", "ENDBLK",
            "0", "BLOCK", "330", "A2", "2", "*U6", "70", "1", "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES", *_insert("*U", "1A"), *_insert("*U", "1B"), *_insert("*U", "1D"),
            *_insert("*U4", "1C"), "0", "ENDSEC", "0", "EOF"]
    path.write_bytes(("\r\n".join(body) + "\r\n").encode())  # Windows 上转出的 DXF 是 CRLF
    return path


def test_anonymous_block_names_are_put_back(tmp_path, monkeypatch):
    """dwgread 读出的块名也是“*U”：只用它给的块记录句柄，名字到 DXF 自己的块记录表里找；名字没有块定义的不算补上。"""
    dxf = _anon_dxf(tmp_path / "a.dxf")
    lines = dxf.read_bytes().split(b"\n")
    assert [h for _line, h in cadfile.broken_blocks(lines)] == [0x1A, 0x1B, 0x1D]
    monkeypatch.setattr(cadfile.dwg, "insert_blocks", lambda src: {0x1A: 0xA2, 0x1C: 0xA1, 0x1D: 0xA3})
    assert cadfile.fix_anonymous_blocks(dxf, tmp_path / "a.dwg") == 1
    lines = dxf.read_bytes().split(b"\n")
    assert [h for _line, h in cadfile.broken_blocks(lines)] == [0x1B, 0x1D]  # 1B 在 DWG 里找不到；1D 的 *U9 没有块定义
    assert lines[lines.index(b"1A\r") + 4] == b"*U6\r"
    assert cadfile.fix_anonymous_blocks(dxf, tmp_path / "a.dwg") == 0  # 再跑一遍不会出错


def test_old_cached_dxf_is_fixed_again(tmp_path, monkeypatch):
    """以前转好的 DXF 是旧办法补的（还是“*U”）：下次用到时再补一遍，算好的范围作废；补过一次就不再读 DWG。"""
    proj = Project.init(tmp_path / "p", "x")
    src = tmp_path / "blob.dwg"
    src.write_bytes(b"AC1015")
    cadfile.out_dir(proj).mkdir(parents=True)
    out = _anon_dxf(cadfile.out_dir(proj) / "结施-04-abababab.dxf")
    stale = out.with_name(out.stem + ".regions.json")
    stale.write_text("{}")
    calls = []
    monkeypatch.setattr(cadfile.dwg, "insert_blocks", lambda s: calls.append(s) or {0x1A: 0xA2, 0x1B: 0xA1})
    assert cadfile.to_dxf(proj, src, "ab" * 32, "结施-04.dwg") == out
    assert [h for _line, h in cadfile.broken_blocks(out.read_bytes().split(b"\n"))] == [0x1D]
    assert not stale.exists() and len(calls) == 1
    cadfile.to_dxf(proj, src, "ab" * 32, "结施-04.dwg")
    assert len(calls) == 1


def test_check_basis_short_values_need_their_own_line(cad_project):
    """轴号、短编号（3、A、H1）：整行就是它才算对得上，不能因为“1:100”里有个 1 就算。"""
    kit = Toolkit(cad_project)
    r14 = kit.cad_file("建施-14")
    base = {"sheet": "建施-14", "rev": r14["rev"], "page_id": r14["page_id"]}
    got = kit.check_basis([{"element": "轴 3", **base, "value": "3"}, {"element": "轴 1", **base, "value": "1"}])
    by = {x["element"]: x for x in got["items"]}
    assert any("单独写着“3”" in c for c in by["轴 3"].get("checked", []))
    assert any("找不到单独写着“1”" in c for c in by["轴 1"]["reasons"])


def test_check_basis_finds_a_sheet_by_its_file_name_and_takes_no_revision(cad_project):
    """以前按文件名暂定时记下的依据（PRJ-3036）找得到图签读出的 S-3036；图本身没有版次（A-150）、依据也没写的，对得上。
    构件的数值在 DWG 页面的文字里也算找到（350x850 和 350×850 一样）。"""
    kit = Toolkit(cad_project)
    page = kit.get_sheet("S-3036")["current_page_id"]
    got = kit.check_basis([{"element": "L30 梁", "sheet": "PRJ-3036", "rev": "A0", "page_id": page, "value": "OFFICE"}])["items"][0]
    assert got["sheet"] == "S-3036" and got["status"] == "对得上", got
    page = kit.get_sheet("A-150")["current_page_id"]
    got = kit.check_basis([{"element": "L12 墙", "sheet": "A-150", "page_id": page, "value": "LEVEL 12"}])["items"][0]
    assert got["status"] == "对得上", got
    assert any("本身没有版次" in c for c in got["checked"])


def test_check_basis_value_in_parts_and_inferred_sizes(cad_project):
    """板标注图上分两行写（建模依据连成一行、顺序可能反过来）：几段都在就算对得上；翻模推断的尺寸直接要复核。"""
    kit = Toolkit(cad_project)
    page = kit.get_sheet("S-3036")["current_page_id"]
    base = {"sheet": "S-3036", "rev": "A0", "page_id": page}
    got = kit.check_basis([{"element": "板", **base, "value": "indicated FLOOR OFFICE"},
                           {"element": "核心筒梁", **base, "value": "推断 300x650（同一区域……；把握中）"}])["items"]
    assert got[0]["status"] == "对得上" and any("分开写着" in c for c in got[0]["checked"])
    assert got[1]["status"] == "要复核" and any("推断的，待确认" in r for r in got[1]["reasons"])
