"""一级识别：只看文件名、文件夹和 PDF 自带的文字，按 ISO 19650 常见写法推出图号、图名、版次、专业、楼层和可能的内容。"""

import functools
import json
import threading

import make_sample
import pytest
from test_codex_executor import FakeCodex, _fake_status
from test_desktop import Client

from bidbench import export, floors, names, ops, recognition, reports, server, state


def pdf(no, title, *body):
    return make_sample.sheet_pdf(no, title, "A", "2026-01-01", list(body), [("A", "2026-01-01", "出图")])


def bare_pdf(*lines, pages=1):
    """只有几行字、没有图签栏的 PDF：模拟识别器认不出图号，像 CAD 直接导出的英文图。"""
    import pymupdf
    doc = pymupdf.open()
    for k in range(pages):
        page = doc.new_page(width=842, height=595)
        for i, t in enumerate(lines):
            page.insert_text((40, 60 + 20 * i), f"{t} {k + 1}" if pages > 1 else t, fontsize=11)
    return doc.tobytes()


@pytest.mark.parametrize("stem,no,rev,status,title", [
    ("PRJ1-ARC-ZZ-00-DR-A-1001_S4_P01_Ground Floor GA Plan", "PRJ1-ARC-ZZ-00-DR-A-1001", "P01", "S4", "Ground Floor GA Plan"),
    ("PRJ1-ARC-ZZ-00-DR-A-1001 - Ground Floor GA Plan [P01]", "PRJ1-ARC-ZZ-00-DR-A-1001", "P01", None, "Ground Floor GA Plan"),
    ("PRJ1-ARC-ZZ-00-DR-A-1001-P02-S4 Ground Floor GA Plan", "PRJ1-ARC-ZZ-00-DR-A-1001", "P02", "S4", "Ground Floor GA Plan"),
    ("PRJ1-STR-ZZ-B1-DR-S-2001 Basement 01 Slab Layout Rev C02", "PRJ1-STR-ZZ-B1-DR-S-2001", "C02", None, "Basement 01 Slab Layout"),
    ("PRJ1-ARC-ZZ-B2-DR-A-1003 Level B2 GA Plan", "PRJ1-ARC-ZZ-B2-DR-A-1003", None, None, "Level B2 GA Plan"),  # B2 是楼层，不是版次
    ("PRJ1-ARC-ZZ-ZZ-DR-A-5001 Section A1", "PRJ1-ARC-ZZ-ZZ-DR-A-5001", None, None, "Section A1"),
    # 图号和图名之间只用了“-”：GROUND 是图名的头一个词，不能丢
    ("PRJ1-ORG-T1-ST-DWG-GF-2302-GROUND FLOOR GA PLAN", "PRJ1-ORG-T1-ST-DWG-GF-2302", None, None, "GROUND FLOOR GA PLAN"),
    ("建施-01 建筑设计总说明", "建施-01", None, None, "建筑设计总说明"),
    ("结施03 二层梁配筋图", "结施-03", None, None, "二层梁配筋图"),
    ("A-101 First Floor Plan Rev B", "A-101", "B", None, "First Floor Plan"),
    ("Ground Floor GA Plan", None, None, None, "Ground Floor GA Plan"),
])
def test_parse_name(stem, no, rev, status, title):
    got = names.parse_name(stem)
    assert (got["no"], got["rev"], got["status"], got["title"]) == (no, rev, status, title)


def test_iso_role_and_folder_discipline():
    assert state.discipline_of("PRJ1-ARC-ZZ-00-DR-A-1001") == "建筑"
    assert state.discipline_of("PRJ1-MEP-ZZ-XX-DR-E-3001") == "电气"
    assert state.discipline_of("SKY1-ABC-ZZ-00-DR-S-0001") == "结构"  # 不再按首字母 S 误判
    assert state.norm_no("PRJ1-STR-ZZ-00-DR-S-0001") == "PRJ1-STR-ZZ-00-DR-S-0001"  # 前导零留着
    assert state.norm_no("建施 -5") == "建施-05"
    assert names.words_discipline("Structure") == "结构" and names.words_discipline("Drawings") is None
    assert names.listed_numbers("建施-07~08、10 详图合订本") == ["建施-07", "建施-08", "建施-10"]


def test_iso_revisions_order():
    assert state.rev_key("P01") < state.rev_key("P02") < state.rev_key("C01") < state.rev_key("C02")
    assert state.rev_key("P01") < state.rev_key("P01.1") < state.rev_key("P02")
    assert state.rev_key("A") < state.rev_key("B")


@pytest.mark.parametrize("title,base,expect", [
    ("Ground Floor GA Plan", 1, {1}), ("Level 01 GA Plan", 1, {2}), ("Level 01 GA Plan", 0, {1}),
    ("Basement 02 Slab Layout", 1, {-2}), ("Roof Plan", 1, {floors.ROOF}), ("Levels 02-04 Typical Plan", 1, {3, 4, 5}),
    ("EXTERNAL SURFACE FINISHES", 1, set()), ("二层梁配筋图", 1, {2}),
])
def test_english_floors(title, base, expect):
    assert floors.parse_floors(title, base) == expect


@pytest.fixture
def named(sheets_project):
    """识别过的中文图 + 只按名字认的英文图（ISO 编号、看不出图号的、合订本、CAD 文件）。"""
    proj = sheets_project({
        "建施-01 建筑设计总说明.pdf": pdf("建施-01", "建筑设计总说明", "详见 PRJ1-STR-ZZ-00-DR-S-2001"),
        "PRJ1-ARC-ZZ-00-DR-A-1001_S4_P01_Ground Floor GA Plan.pdf": bare_pdf("GROUND FLOOR GA PLAN", "PRJ1-ARC-ZZ-00-DR-A-1001"),
        "PRJ1-ARC-ZZ-05-DR-A-1006_P02_Level 05 GA Plan.pdf": bare_pdf("some text"),
        "PRJ1-STR-ZZ-00-DR-S-2001 Ground Floor Slab Layout [C01].pdf": bare_pdf("SLAB"),
        "Mechanical Riser Diagram.pdf": bare_pdf("riser"),
        "PRJ1-ARC-ZZ-RF-DR-A-1099_Roof Plan.dwg": b"AC1032 placeholder",
        "建施-07~08 详图合订本.pdf": bare_pdf("binder page", pages=2),
    })
    return proj


def test_first_guess_sheets(named):
    st = state.build(named)
    by = {s["no"]: s for s in st["sheets"]}
    g = by["PRJ1-ARC-ZZ-00-DR-A-1001"]
    assert g["status"] == "guess" and g["title"] == "Ground Floor GA Plan" and g["current_rev"] == "P01"
    assert g["fields"]["no"]["conf"] == "high"  # 文件名里有，PDF 文字里也有
    assert {k: g["fields"]["discipline"][k] for k in ("value", "conf", "src")} == {"value": "建筑", "conf": "high", "src": "图号里的角色代码"}
    assert g["fields"]["no"]["where"]["path"].endswith("Ground Floor GA Plan.pdf")  # 每项都带出处
    assert g["floors"] == [1] and g["fields"]["floors"]["conf"] == "low"
    assert "建筑平面图：可能有房间布置" in g["fields"]["content"]["value"] and g["fields"]["content"]["conf"] == "low"
    l5 = by["PRJ1-ARC-ZZ-05-DR-A-1006"]
    assert l5["fields"]["no"]["conf"] == "mid" and l5["floors"] == [6]  # 有 Ground Floor：英式，Level 05 是 6F
    assert by["PRJ1-STR-ZZ-00-DR-S-2001"]["discipline"] == "结构"
    riser = by["文件：Mechanical Riser Diagram"]  # 看不出图号也出一张，把握低
    assert riser["conf"] == "low" and riser["discipline"] == "暖通" and riser["fields"]["no"]["value"] is None
    roof = by["PRJ1-ARC-ZZ-RF-DR-A-1099"]  # 只有 CAD 文件
    assert roof["pages"] == [] and roof["cad"] and roof["floors"] == [floors.ROOF]
    assert by["建施-07"]["conf"] == "low" and by["建施-08"]["pages"] == by["建施-07"]["pages"]  # 合订本文件名列出的图号
    assert st["guess"]["nameless"] == 1 and st["guess"]["sheets"] >= 6
    # 被引用、但只是还没看图识别的图：不报缺图
    assert not [i for i in st["issues"] if i["kind"] == "缺图"]
    up = next(f for g in st["facts"]["groups"] for f in g["facts"] if f["key"] == "地上层数")
    assert up["conf"] == "low" and up["value"] == "6层" and up.get("guess")  # 按图名推测，低置信
    assert "按文件名暂定" in reports.register_report(st)
    from bidbench.store import add_decision
    add_decision(named, "fact", "地上层数", "6层")  # 人工确认推测的值
    up = next(f for g in state.build(named)["facts"]["groups"] for f in g["facts"] if f["key"] == "地上层数")
    assert up["conf"] == "confirmed" and up["value"] == "6层"


def test_guess_not_exported(named, tmp_path):
    st = state.build(named)
    out = export.export_current(named, st, tmp_path / "out")
    listing = (out / "现行图纸清单.md").read_text(encoding="utf-8")
    assert "按文件名暂定、还没看图识别的图" in listing and not list(out.rglob("PRJ1*.pdf"))


def test_file_name_vs_title_block(sheets_project):
    """看图识别出的版次和文件名写的不一样：列成问题，可以列入答疑或以图签为准。"""
    proj = sheets_project({"建施-02 C02 一层平面图.pdf": make_sample.sheet_pdf(
        "建施-02", "一层平面图", "C03", "2026-01-01", [], [("C03", "2026-01-01", "出图")])})
    st = state.build(proj)
    iss = next(i for i in st["issues"] if i["kind"] == "文件名不符")
    assert "文件名写 C02，图签是 C03" in iss["detail"] and iss["target"] == {"sheet": "建施-02"}
    from bidbench.store import add_decision
    add_decision(proj, "name_check", iss["options"][0]["key"], "ask")
    st = state.build(proj)
    assert "文件名与图签不一致" in reports.qa_report(st)
    add_decision(proj, "name_check", iss["options"][0]["key"], "ignore")
    assert not [i for i in state.build(proj)["issues"] if i["kind"] == "文件名不符"]


def test_newer_file_not_yet_recognized(sheets_project):
    proj = sheets_project({"建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图")})
    from bidbench import ingest
    folder = proj.root.parent / "补发"
    folder.mkdir()
    (folder / "建施-02 B 一层平面图.pdf").write_bytes(bare_pdf("新版"))
    ingest.receive(proj, folder, "2026-02-01")  # 收了，还没识别
    st = state.build(proj)
    s = next(x for x in st["sheets"] if x["no"] == "建施-02")
    assert s["status"] != "guess" and s["pending_files"][0]["rev"] == "B" and s["pending_files"][0]["newer"]
    assert any("文件名显示有更新的 B 版" in n for n in s["notes"])
    assert any(i["id"] == "newer_files" for i in st["issues"])


def test_recognize_on_demand(named, monkeypatch):
    """按需识别：只识别点中的几页。"""
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: _fake_status())
    fake = FakeCodex()
    monkeypatch.setattr(ops, "recognize_codex", functools.partial(ops.recognize_codex, runner=fake))
    monkeypatch.setattr(ops, "classify", functools.partial(ops.classify, runner=FakeCodex(lambda p, n: ({"sheets": [], "facts": []}, None))))
    st = state.build(named)
    want = next(s for s in st["sheets"] if s["no"] == "PRJ1-ARC-ZZ-00-DR-A-1001")["pages"]
    httpd = server.serve(named, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        assert c.post("/api/job", {"kind": "recognize", "executor": "codex", "pages": ["../x"]})["_status"] == 400
        j = c.job("recognize", executor="codex", pages=want)
        assert j["status"] == "done" and j["result"]["done"] == 1, j
    finally:
        httpd.shutdown()
    task = recognition.latest_task(named)
    items = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    assert [it["page_id"] for it in items] == want


def test_project_code_is_not_a_discipline_letter():
    """ATB-3008：第一段 ATB 是项目代码，不按首字母 A 认成建筑；放在 Structure 文件夹里的按文件夹认结构。"""
    assert state.discipline_of("ATB-3008") == "其他"
    assert state.discipline_of("A-101") == "建筑" and state.discipline_of("S2-01") == "结构"
    assert names.words_discipline("Structure - DSP") == "结构" and names.words_discipline("Gambar Struktur") == "结构"
    assert names.words_discipline("For Information Structure_CAD") == "结构"
