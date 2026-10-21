"""读 DWG、DXF（docs/图纸字段.md 9.7）：LibreDWG 的输出怎么读、一个文件怎么分图、读出的图怎么进台账。"""

import json
import os
import stat
from pathlib import Path

import jsonschema
import make_sample
import pytest
from conftest import _recognize

from bidbench import cadfile, dwg, export, ingest, navigate, ops, state
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
    assert dwg.insert_blocks(f) == {65: 47, 76: 47}  # 每个块参照用哪条块记录（句柄）：给 dwg2dxf 写丢的匿名块名补回

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


def test_revit_frame_labels_in_the_block_and_values_on_the_layout(cad_project):
    """Revit 导出的图框：“Sheet No.”这些标签在块定义里（分两行写），值散放在标签下面那一格，图号写成“-S-3036”；
    文件名用项目代号 PRJ-3036。修改记录 A0 那一行写死在图框块里、表头上面。文件名只是前缀不同，不报“文件名不符”。"""
    st = state.build(cad_project)
    sheets = {s["no"]: s for s in st["sheets"]}
    s = sheets["S-3036"]
    assert s["status"] != "guess" and s["title"] == "TYPICAL OFFICE FLOOR PLAN" and s["current_rev"] == "A0"
    tb = json.loads(cad_project.connect().execute(
        "SELECT data FROM recognitions WHERE page_id=?", (s["current_page"],)).fetchone()[0])["title_block"]
    assert tb["scale"]["value"] == "As indicated" and tb["issue_date"]["value"] == "03/05/24"  # 不是旁边格子里的 Rn
    assert [(r["revision"], r["date"], r["description"]) for r in tb["revision_history"]] == [("A0", "15/12/2025", "FOR INFORMATION")]
    assert "PRJ-3036" not in sheets
    assert not [i for i in st["issues"] if i["id"] == "name:S-3036"]
    # dwg2dxf 的视口：编号都是 1，布局本身是 12×9 的默认视口（看的高 12）、夹在中间；主视口看到的模型空间字归这张图
    text = cad_project.connect().execute("SELECT text FROM pages WHERE page_id=?", (s["current_page"],)).fetchone()[0]
    assert "KL12 300x700" in text and "DETAIL 1" in text
    assert "S-3002C" not in sheets  # 模型空间里留下来的旧图签块：布局有自己的图签，不算一张图


def test_autocad_recover_copy_is_the_same_drawing(cad_project):
    """AutoCAD 修复出来的副本（图名_recover.dxf）和原文件同版次、内容有一点不同：算同一份，用原文件，不报冲突、不报改图未升版。"""
    st = state.build(cad_project)
    s = next(x for x in st["sheets"] if x["no"] == "S-3008")
    assert s["status"] != "conflict" and [v["state"] for v in s["versions"]] == ["现行"]
    assert all("_recover" not in p for p in s["versions"][0]["paths"][:1])
    page = cad_project.connect().execute(
        "SELECT o.path FROM pages p JOIN occurrences o ON o.sha256 = p.file_sha256 WHERE p.page_id=?", (s["current_page"],)).fetchone()[0]
    assert not page.endswith("_recover.dxf")
    assert not [i for i in st["issues"] if i["target"].get("sheet") == "S-3008" and i["kind"] in ("版本冲突", "改图未升版")]


def test_cad_xrefs_and_callout_marks(cad_project):
    """DWG 给出的两种关联：共用的外部参照（附着的、绑定进来的都算），索引符号里的详图编号。"""
    st = state.build(cad_project)
    shared = navigate.shared_xrefs(st)
    assert shared["标准层柱网"] == ["建施-03", "建施-04"]  # 建施-03 的现行页是 PDF，参照从配对的 DXF 并过来
    assert shared["核心筒-1"] == ["PRJ1-A-701", "PRJ1-A-702"]
    edge = next(e for e in st["edges"] if (e["from"], e["to"]) == ("建施-14", "建施-15"))
    assert (edge["form"], edge["mark"]) == ("详图索引", "3")


def test_bound_xref_names():
    assert dwg._xref_blocks(set(), ["TK", "CORE", "CORE$0$WALL", "*Model_Space"]) == {"CORE"}
    assert dwg._xref_blocks({"GRID"}, ["GRID", "TK"]) == {"GRID"}
    assert dwg._xref_blocks(set(), ["X$0$WALL"]) == set()  # 参照本身的块没了，不算


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
    # 读的规则升级了：按旧规则读的文件算没读，下一步提“读 DWG”，读的时候自动重读
    with proj.connect() as con:
        con.execute("UPDATE cad_reads SET version = 'dwg-1'")
    st = state.build(proj)
    assert st["cad_read"]["unread"] == 1 and [t["id"] for t in st["guide"]["tasks"] if t["id"] == "read_dwg"] == ["read_dwg"]
    assert ops.read_cad(proj, log=lambda s: None)["files"] == 1
    assert proj.connect().execute("SELECT version FROM cad_reads").fetchone()[0] == dwg.INSTRUCTION_VERSION
    assert state.build(proj)["cad_read"]["unread"] == 0


def _tool(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.skipif(os.name == "nt", reason="假的 dwgread、dwg2dxf 是 shell 脚本")
def test_dwgread_cut_short_falls_back_to_dxf(tmp_path, monkeypatch):
    """dwgread 读到一半崩了（JSON 没写完）：不当作读全，先转 DXF 读；转不了再读一次，还不全就列“DWG 没读全”。"""
    lines = (DATA / "layouts.json").read_text(encoding="utf-8").splitlines(True)
    cut = tmp_path / "cut.json"
    cut.write_text("".join(lines[:2000]), encoding="utf-8")  # 布局上的图签在后面，没读到
    full = DATA / "layouts.json"
    reader = _tool(tmp_path / "dwgread", f"cat '{cut}'\nexit 127\n")
    monkeypatch.setenv("BIDBENCH_DWGREAD", str(reader))
    f = tmp_path / "a.dwg"
    f.write_bytes(b"AC1015")
    d = dwg.read_dwg(f)
    assert d.partial and not any(s.fields.get("drawing_no") for s in dwg.split_sheets(d))
    # 崩的地方每次不一样：第二次读全了就用第二次
    count = tmp_path / "n"
    _tool(reader, f"if [ -e '{count}' ]; then cat '{full}'; else touch '{count}'; cat '{cut}'; exit 127; fi\n")
    d = dwg.read_dwg(f, tries=2)
    assert not d.partial and len(dwg.split_sheets(d)) == 3

    _tool(reader, f"cat '{cut}'\nexit 127\n")
    dxf = tmp_path / "full.dxf"
    dxf.write_bytes(make_sample.cad_dxf([{"no": "建施-01", "title": "一层平面图", "rev": "B", "date": "2026-01-01"}]))
    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(_tool(tmp_path / "dwg2dxf", f"cp '{dxf}' \"$3\"\n")))
    folder = tmp_path / "图纸"
    folder.mkdir()
    (folder / "建施-01 一层平面图.dwg").write_bytes(b"AC1032" + b"\0" * 64)
    proj = Project.init(tmp_path / "p", "DWG 项目")
    ops.receive(proj, folder, "2026-10-01", av=False, log=lambda s: None)
    st = state.build(proj)
    s1 = next(s for s in st["sheets"] if s["no"] == "建施-01")
    assert s1["status"] != "guess" and s1["current_rev"] == "B"  # 图签从转出的 DXF 读到
    assert proj.connect().execute("SELECT status FROM cad_reads").fetchone()[0] == "ok"
    assert proj.connect().execute("SELECT reader FROM cad_reads").fetchone()[0].endswith(" dwg2dxf")
    assert list(cadfile.out_dir(proj).glob("*.dxf"))  # 转好的 DXF 留着，翻模直接用
    assert not [i for i in st["issues"] if i["kind"] == "DWG 没读全"]

    # 转不了 DXF：读出的部分照用，列一条提醒
    for x in cadfile.out_dir(proj).iterdir():
        x.unlink()
    monkeypatch.setenv("BIDBENCH_DWG2DXF", str(tmp_path / "没有"))
    logs = []
    ops.read_cad(proj, again=True, log=logs.append)
    assert any("只读出一部分" in x for x in logs)
    assert proj.connect().execute("SELECT status FROM cad_reads").fetchone()[0] == "partial"
    st = state.build(proj)
    assert [i["title"] for i in st["issues"] if i["kind"] == "DWG 没读全"] == ["没读全：建施-01 一层平面图.dwg"]
    # 读全以后提醒去掉
    _tool(reader, f"cat '{full}'\n")
    ops.read_cad(proj, again=True, log=lambda s: None)
    assert not [i for i in state.build(proj)["issues"] if i["kind"] == "DWG 没读全"]


def test_cad_reads_from_before_the_version_column_are_read_again(tmp_path):
    """第 1 版数据库的 cad_reads 没有规则版本：升级后这些文件都按旧规则读的算，下次读 DWG 时重读。"""
    import sqlite3

    from bidbench import store
    proj = Project.init(tmp_path / "p", "旧项目")
    con = sqlite3.connect(proj.db_path)
    con.executescript("DROP TABLE cad_reads; CREATE TABLE cad_reads(sha256 TEXT PRIMARY KEY, status TEXT NOT NULL,"
                      " reader TEXT NOT NULL, sheets INTEGER NOT NULL, detail TEXT, read_at TEXT NOT NULL);"
                      "INSERT INTO files(sha256, stored_path, size, ext, kind) VALUES ('s1', 'files/a.dxf', 1, '.dxf', 'cad');"
                      "INSERT INTO occurrences(sha256, batch_seq, path, depth) VALUES ('s1', 1, 'a.dxf', 0);"
                      "INSERT INTO cad_reads VALUES ('s1', 'ok', 'LibreDWG', 1, NULL, '2026-10-01');"
                      "PRAGMA user_version = 1;")
    con.commit()
    con.close()
    with proj.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        assert con.execute("SELECT version FROM cad_reads").fetchone()[0] is None
        assert [r[0] for r in dwg.unread(con)] == ["s1"]


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


def _pairs(*groups) -> list[tuple[str, str]]:
    flat = [x for g in groups for x in g]
    return [(str(flat[i]), str(flat[i + 1])) for i in range(0, len(flat), 2)]


def test_split_title_blocks_and_nested_grid_bubbles():
    """实测的印尼图：图签拆成两个块（ATT KOP 写图号 NOGBR、图名；STATUS KOP 写版次 REV、日期 TGL），合成一张图；
    轴号是轴网块里套着的轴号圈块的属性（AS），也读出来，check_basis 才能按轴号核对。"""
    def ins(name, x, y, attrs=(), space="S-2001", rot=0):
        out = [0, "INSERT", 8, "0", *([67, 1, 410, space] if space else []), 66, 1 if attrs else 0, 2, name,
               10, x, 20, y, 30, 0, 50, rot]
        for k, (tag, val) in enumerate(attrs):
            out += [0, "ATTRIB", 8, "0", 10, x + 10, 20, y + 5 * k, 30, 0, 40, 2.5, 1, val, 2, tag, 70, 0]
        return out + ([0, "SEQEND", 8, "0"] if attrs else [])
    pairs = _pairs(
        [0, "SECTION", 2, "BLOCKS"],
        [0, "BLOCK", 8, "0", 2, "ATT KOP", 70, 2, 10, 0, 20, 0],
        [0, "LINE", 8, "0", 10, 0, 20, 0, 11, 840, 21, 0], [0, "LINE", 8, "0", 10, 840, 20, 0, 11, 840, 21, 594],
        [0, "ENDBLK", 8, "0"],
        [0, "BLOCK", 8, "0", 2, "BUBBLE", 70, 2, 10, 0, 20, 0], [0, "ENDBLK", 8, "0"],
        [0, "BLOCK", 8, "0", 2, "*U4", 70, 1, 10, 0, 20, 0],  # 动态块：一根轴线，两头各一个轴号圈
        [0, "LINE", 8, "CENTER", 10, 0, 20, 0, 11, 0, 21, 30000],
        ins("BUBBLE", 0, -800, [("AS", "3")], space=None), ins("BUBBLE", 0, 30800, [("AS", "3")], space=None),
        [0, "ENDBLK", 8, "0"],
        [0, "ENDSEC"], [0, "SECTION", 2, "ENTITIES"],
        ins("ATT KOP", 0, 0, [("NOGBR", "S-2001"), ("JUDUL2", "STRUCTURAL SECTION"), ("SCALE", "1:200")]),
        ins("STATUS KOP", 700, 20, [("REV", "A0"), ("TGL", "2026-09-30"), ("DES", "FOR TENDER")]),
        ins("*U4", 100000, 0, space=None),
        [0, "ENDSEC"], [0, "EOF"])
    d = dwg.from_dxf_pairs(pairs)
    sheets = dwg.split_sheets(d)
    assert len(sheets) == 1
    f = sheets[0].fields
    assert f["drawing_no"] == "S-2001" and f["revision"] == "A0" and f["issue_date"] == "2026-09-30"
    bubbles = [i for i in d.inserts if i.nested]
    assert [(round(i.x), round(i.y), i.attrs[0][1]) for i in bubbles] == [(100000, -800, "3"), (100000, 30800, "3")]
    assert not any(dwg.is_title_block(i) for i in bubbles)


def test_layout_sheet_gets_model_text_seen_by_its_viewport():
    """布局按视口看模型空间：视口看得到的字算布局那张图的。布局有自己的图签时，模型空间里留下来的图签块（复制图框带过来的
    旧图号 S-3002C）不算一张图；布局只有布局名时，模型空间那张照样算，字也照样留在上面；图签块里没有线的，cad_file 说它只是个标签。"""
    d = dwg.Drawing(spaces=[dwg.MODEL, "S-2001"])
    d.inserts = [dwg.Insert("TAG", 5000, 5000, 1, 1, 0, dwg.MODEL, [("DRAWING_NO", "S-3002C", 5000, 5000)]),
                 dwg.Insert("TB", 0, 0, 1, 1, 0, "S-2001", [("DRAWING_NO", "S-2001", 90, 5)])]
    d.texts = [dwg.Text("A", 50, 150, dwg.MODEL), dwg.Text("1", 150, 50, dwg.MODEL), dwg.Text("KOLOM K1", 5100, 5100, dwg.MODEL)]
    d.viewports = [dwg.Viewport("S-2001", 50, 50, 100, 100, 100, 100, 200)]
    by = {s.fields["drawing_no"]: s for s in dwg.split_sheets(d)}
    assert list(by) == ["S-2001"]
    layout = sorted(t.text for t in by["S-2001"].texts)
    assert layout == ["1", "A"] and all(t.space == "S-2001" for t in by["S-2001"].texts)

    d.inserts = d.inserts[:1]  # 布局上没有图签，图号只能用布局名
    by = {s.fields["drawing_no"]: s for s in dwg.split_sheets(d)}
    assert set(by) == {"S-3002C", "S-2001"} and by["S-2001"].how == "布局名"
    assert {"A", "1", "KOLOM K1"} <= {t.text for t in by["S-3002C"].texts}
    assert "小标签" in cadfile._region(d, by["S-3002C"])["note"]


def test_revision_label_takes_a_revision_not_the_next_column_header():
    """Revit 导出的图签是普通文字，修改记录表头“REV | DESCRIPTION | DATE”：版次取 REV 下面的 C，不取右边的表头。"""
    T = dwg.Text
    texts = [T("Sheet Number", 0, 100, "L", 2.5), T("S-01", 0, 96, "L", 2.5),
             T("REV", 0, 50, "L", 2.5), T("DESCRIPTION", 10, 50, "L", 2.5), T("DATE", 60, 50, "L", 2.5),
             T("C", 0, 46, "L", 2.5), T("FOR CONSTRUCTION", 10, 46, "L", 2.5)]
    f, _c = dwg.label_fields(texts)
    assert f["drawing_no"] == "S-01" and f["revision"] == "C"
    assert dwg.label_fields([T("Revised by Lee", 0, 0, "L")])[0].get("revision") is None
    assert dwg.label_fields([T("Rev: P2", 0, 0, "L")])[0]["revision"] == "P2"
