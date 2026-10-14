"""读 DWG、DXF（docs/图纸字段.md 9.6）：LibreDWG 的输出怎么读、一个文件怎么分图、读出的图怎么进台账。"""

import json
import os
import stat
from pathlib import Path

import jsonschema
import make_sample
import pytest
from conftest import _recognize

from bidbench import dwg, export, ingest, ops, state
from bidbench.store import Project

DATA = Path(__file__).parent / "data" / "dwg"
SCHEMA = json.loads((Path(dwg.__file__).parent / "spec" / "schema.json").read_text(encoding="utf-8"))


def _libredwg(name: str) -> dwg.Drawing:
    """按 dwgread 输出的样子逐行读（边读边筛）。"""
    with open(DATA / name, "rb") as f:
        meta, objs = dwg.iter_json(f)
        return dwg.from_objects(objs, meta)


def _recs(d: dwg.Drawing) -> list[dict]:
    sheets = dwg.split_sheets(d)
    out = []
    for n, s in enumerate(sheets, 1):
        r = dwg.recognition(f"abcdef012345-p{n:03d}", "a" * 64, "x.dwg", n, len(sheets), s, d.reader)
        jsonschema.validate(r, SCHEMA)
        out.append(r)
    return out


def _no(r):
    return r["title_block"]["drawing_no"]["value"]


# ---------------- LibreDWG 的输出 ----------------

def test_libredwg_layouts():
    """一个 DWG 几个布局：每个布局一张图；图签属性名认不出时按值认图号；模型空间的字按视口归到布局。"""
    d = _libredwg("layouts.json")
    assert d.reader.startswith("LibreDWG") and d.version == "AC1015"
    recs = _recs(d)
    assert [_no(r) for r in recs] == ["PRJ1-A-701", "PRJ1-A-702", "A-150"]
    a701, a702, a150 = recs
    assert a701["title_block"]["drawing_title"]["value"] == "ENLARGED CORE PLAN A"  # JUDUL1 + JUDUL2
    assert a701["title_block"]["scale"]["value"] == "1:50"  # SKALA
    assert a701["title_block"]["drawing_no"]["confidence"] == dwg.NEAR_CONF  # 按值认的，把握中
    assert [x["target_drawing_no"] for x in a701["references"]] == ["A-310"]  # 模型空间里“REFER TO A-310”在 701 的视口里
    assert a701["references"][0]["bbox"] is not None
    assert a702["references"] == []
    assert a150["title_block"]["drawing_title"]["value"] == "GENERAL ARRANGEMENT PLAN - LEVEL 12"  # 图签是普通文字
    assert "图签文字" in a150["content"]["summary"]


def test_libredwg_model_space():
    """模型空间里排两张图，各有图签块；索引符号和“详见”文字是引用；只有一行字的布局不算一张图。"""
    recs = _recs(_libredwg("model.json"))
    assert [_no(r) for r in recs] == ["建施-03", "建施-04"]
    targets = sorted((x["target_drawing_no"], x["ref_type"]) for x in recs[0]["references"])
    assert targets == [("建施-01", "文字引用"), ("建施-12", "文字引用"), ("建施-12", "详图索引")]
    assert recs[1]["references"] == []


def test_iter_json_tolerates_bad_bytes_and_skips_unneeded():
    lines = [b"{\n", b'  "created_by": "LibreDWG 0.13.3",\n', b'  "OBJECTS": [\n',
             b"    {\n", b'      "entity": "TEXT",\n', b'      "entmode": 2,\n', b'      "ins_pt": [1.0,2.0],\n',
             b'      "text_value": "ROOM \xff\xfe 01"\n', b"    },\n",
             b"    {\n", b'      "entity": "HATCH",\n', b'      "huge": [1,2,3]\n', b"    },\n",
             b"    {\n", b'      "entity": "TEXT",\n', b'      "text_value": "broken\n', b"    },\n",
             b"  ]\n", b"}\n"]
    meta, objs = dwg.iter_json(lines)
    got = list(objs)
    assert meta["reader"] == "LibreDWG 0.13.3"
    assert len(got) == 1 and got[0]["text_value"].startswith("ROOM ")  # 坏字节换成替代符，坏对象跳过


@pytest.mark.skipif(os.name == "nt", reason="假的 dwgread 是 shell 脚本")
def test_read_dwg_with_fake_tool(tmp_path, monkeypatch):
    tool = tmp_path / "dwgread"
    tool.write_text(f"#!/bin/sh\ncat '{DATA / 'layouts.json'}'\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BIDBENCH_DWGREAD", str(tool))
    f = tmp_path / "a.dwg"
    f.write_bytes(b"AC1015")
    assert dwg.find_tool() == str(tool)
    assert len(dwg.split_sheets(dwg.read_dwg(f))) == 3

    tool.write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(dwg.ReadError, match="读不出"):
        dwg.read_dwg(f)

    tool.write_text("#!/bin/sh\nsleep 5\n")
    monkeypatch.setattr(dwg, "timeout_for", lambda size: 0.5)
    with pytest.raises(dwg.ReadError, match="还没读完"):
        dwg.read_dwg(f)


@pytest.mark.skipif(dwg.find_tool() is None, reason="这台电脑没装 LibreDWG")
def test_real_libredwg_reads_fixture_dwgs():
    assert [_no(r) for r in _recs(dwg.read_dwg(DATA / "layouts.dwg"))] == ["PRJ1-A-701", "PRJ1-A-702", "A-150"]
    assert [_no(r) for r in _recs(dwg.read_dwg(DATA / "model.dwg"))] == ["建施-03", "建施-04"]


# ---------------- 认图签、找引用 ----------------

@pytest.mark.parametrize("tag,want", [
    ("图号", ("drawing_no", 0)), ("DWG_NO", ("drawing_no", 0)), ("Sheet Number", ("drawing_no", 0)),
    ("图名2", ("drawing_title", 2)), ("JUDUL1", ("drawing_title", 1)), ("REV", ("revision", 0)),
    ("KD0105", (None, 0)), ("DATE2", (None, 0)),
])
def test_tag_role(tag, want):
    assert dwg.tag_role(tag) == want


def test_find_numbers_skips_door_and_member_marks():
    assert dwg.find_numbers("未注明者详见建施-12，门 M-1021，KL-120") == ["建施-12"]
    assert dwg.find_numbers("REFER TO A-310 FOR SECTIONS") == ["A-310"]
    assert dwg.find_numbers("见 PRJ1-ARC-ZZ-00-DR-A-1001") == ["PRJ1-ARC-ZZ-00-DR-A-1001"]
    assert dwg.find_numbers("A-210", callout=True) == ["A-210"]  # 索引符号里的短图号照样算
    assert dwg.looks_like_no("LDI-A-224") and not dwg.looks_like_no("1:100") and not dwg.looks_like_no("2026-10-01")


def test_dxf_writer_and_reader_agree():
    d = dwg.from_dxf_pairs([])
    assert d.spaces == [dwg.MODEL]
    data = make_sample.cad_dxf([{"no": "建施-01", "title": "一层平面图", "rev": "A", "date": "2026-01-01",
                                 "texts": [(10, 10, "详见建施-02")]}])
    lines = data.decode("utf-8").splitlines()
    d = dwg.from_dxf_pairs([(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)])
    recs = _recs(d)
    assert _no(recs[0]) == "建施-01" and recs[0]["title_block"]["drawing_no"]["confidence"] == dwg.ATTR_CONF
    assert [x["target_drawing_no"] for x in recs[0]["references"]] == ["建施-02"]


# ---------------- 进台账 ----------------

@pytest.fixture
def cad_project(tmp_path):
    root, date = make_sample.build_cad(tmp_path / "in")
    proj = Project.init(tmp_path / "p", "CAD 项目")
    ops.receive(proj, root, date, av=False, log=lambda s: None)
    _recognize(proj)
    return proj


def test_cad_sheets_join_ledger(cad_project):
    st = state.build(cad_project)
    sheets = {s["no"]: s for s in st["sheets"]}
    assert {"建施-03", "建施-04", "建施-13", "建施-14", "PRJ1-A-701", "PRJ1-A-702", "A-150"} <= set(sheets)
    for no in ("建施-13", "建施-14", "PRJ1-A-701", "A-150"):  # 只有 DXF 的图：读出图号就不是暂定
        assert sheets[no]["status"] != "guess", no
    assert sheets["建施-13"]["fields"]["no"]["src"] == "DWG 图签"
    # 建施-03：PDF 和 DXF 同为 B 版，配成一份，不报“改图未升版”；看图、导出用 PDF；DXF 里的引用也算上
    assert not [i for i in st["issues"] if i["id"].startswith(("rev_same:", "rev_dup:")) and "建施-03" in i["id"]]
    assert cad_project.connect().execute("SELECT f.ext FROM pages p JOIN files f ON f.sha256 = p.file_sha256"
                                         " WHERE p.page_id = ?", (sheets["建施-03"]["current_page"],)).fetchone()[0] == ".pdf"
    assert ("建施-03", "建施-13") in {(e["from"], e["to"]) for e in st["edges"]}
    # 建施-04：PDF 是 A 版，DXF 是 B 版，DXF 现行
    assert sheets["建施-04"]["current_rev"] == "B"
    # 建施-14 的索引符号指向缺的建施-15
    assert sheets["建施-15"]["status"] == "missing"
    # 读不出的文件：提醒一条，按文件名暂定
    bad = [i for i in st["issues"] if i["kind"] == "DWG 读不出"]
    assert len(bad) == 1 and "坏文件.dxf" in bad[0]["title"]
    assert st["cad_read"]["unread"] == 0


def test_export_current_copies_cad_original(cad_project, tmp_path):
    st = state.build(cad_project, with_diff=False)
    out = export.export_current(cad_project, st, tmp_path / "out")
    files = {p.name for p in out.rglob("*") if p.is_file()}
    assert any(n.startswith("建施-13") and n.endswith(".dxf") for n in files)
    assert any(n.startswith("建施-03") and n.endswith(".pdf") for n in files)


def test_unread_dwg_waits_for_libredwg(tmp_path, monkeypatch):
    """没装 LibreDWG：DWG 只按文件名暂定，下一步不提读 DWG；装好以后“读 DWG”补读。"""
    monkeypatch.setenv("BIDBENCH_DWGREAD", str(tmp_path / "没有这个程序"))
    folder = tmp_path / "图纸"
    folder.mkdir()
    (folder / "建施-01 一层平面图.dwg").write_bytes(b"AC1032" + b"\0" * 64)
    proj = Project.init(tmp_path / "p", "DWG 项目")
    logs = []
    ops.receive(proj, folder, "2026-10-01", av=False, log=logs.append)
    assert any("没有找到 LibreDWG" in x for x in logs)
    st = state.build(proj)
    assert st["sheets"][0]["status"] == "guess"
    assert st["cad_read"] == {"unread": 1, "tool": False}
    assert not [t for t in st["guide"]["tasks"] if t["id"] == "read_dwg"]

    if os.name == "nt":
        return
    tool = tmp_path / "dwgread"
    tool.write_text(f"#!/bin/sh\ncat '{DATA / 'model.json'}'\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BIDBENCH_DWGREAD", str(tool))
    st = state.build(proj)
    assert [t["action"] for t in st["guide"]["tasks"] if t["id"] == "read_dwg"] == ["read_dwg"]
    r = ops.read_cad(proj, log=lambda s: None)
    assert r == {"files": 1, "sheets": 2, "failed": 0, "no_tool": 0}
    st = state.build(proj)
    assert {s["no"] for s in st["sheets"] if s["status"] not in ("guess", "missing")} == {"建施-03", "建施-04"}
    assert st["cad_read"]["unread"] == 0
    # 重读：先删掉旧的页再写
    assert ops.read_cad(proj, again=True, log=lambda s: None)["sheets"] == 2
    assert proj.connect().execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 2


def test_cad_pages_are_not_sent_to_vision(cad_project):
    from bidbench import recognition
    with cad_project.connect() as con:
        rows = recognition.pages_to_recognize(con, scope="all")
    assert rows and all(r["stored_path"].endswith(".pdf") for r in rows)


def test_cli_read_dwg_is_logged(cad_project, capsys):
    from bidbench import cli, oplog
    cli.main(["-p", str(cad_project.root), "read-dwg"])
    assert "没有要读的" in capsys.readouterr().out
    assert any(e["title"].startswith("读 DWG") for e in oplog.read(cad_project))


def test_layout_name_as_sheet_number():
    """本地设计院的 DWG 常把布局名写成图号：图签里认不出时用布局名（把握中）。"""
    data = make_sample.cad_dxf(layouts=[("A-224", None, [(0, 0, "GENERAL ARRANGEMENT PLAN"), (0, 10, "LEVEL 14"),
                                                         (0, 20, "OFFICE")])])
    lines = data.decode("utf-8").splitlines()
    recs = _recs(dwg.from_dxf_pairs([(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]))
    assert _no(recs[0]) == "A-224" and "布局名" in recs[0]["content"]["summary"]
