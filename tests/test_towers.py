"""图纸身份 = 塔楼 + 图号（docs/图纸字段.md 9.1）：同图号不同楼各算各的，缺图按楼查，以前的决定原样带过来。"""

import make_sample
import pytest
from conftest import _recognize

from bidbench import ingest, state, towers
from bidbench.assistant import Toolkit, ToolError
from bidbench.store import Project, add_decision


@pytest.mark.parametrize("name,want", [
    ("T1", "T1"), ("T01", "T1"), ("T1-建筑", "T1"), ("T1.zip", "T1"), ("Tower 2", "T2"), ("塔楼3", "T3"), ("2号塔楼", "T2"),
    ("1#楼", "1#楼"), ("1号楼建筑", "1#楼"), ("十二号楼", "12#楼"), ("A座", "A座"), ("B栋", "B座"),
    ("裙楼", "裙楼"), ("Podium", "裙楼"), ("地下室", "地下室"), ("Basement", "地下室"), ("公共部分", "公共"), ("General", "公共"),
    ("建筑", None), ("批次#1", None), ("Tower Crane", None), ("Drawings", None),
])
def test_tower_from_folder(name, want):
    assert towers.from_folder(name) == want


@pytest.mark.parametrize("text,want", [
    ("1#楼一层平面图", "1#楼"), ("1号楼梯详图", None), ("2#楼板配筋图", None), ("Tower A Level 05 GA Plan", "TA"),
    ("PRJ1-ARC-ZZ-00-DR-A-1001_T1_Ground Floor", None),  # T1 也可能是版次，文件名里不认
    ("地下室顶板平面图", None), ("A座立面图", "A座"),
])
def test_tower_from_text(text, want):
    assert towers.from_text(text) == want


def test_tower_from_iso_and_pdf():
    assert towers.from_iso("PRJ1-ARC-T1-05-DR-A-1001") == "T1"
    assert towers.from_iso("PRJ1-ARC-ZZ-05-DR-A-1001") is None  # ZZ：多个分区
    assert towers.from_pdf_text("工程名称 示例 子项名称：1#楼") == "1#楼"
    assert towers.from_pdf_text("子项：办公楼") == "办公楼"
    assert towers.alias_pairs({"T1", "T2", "1#楼"}) == [("T1", "1#楼")]


def _receive(tmp_path, root, date, name="双塔项目", proj=None):
    proj = proj or Project.init(tmp_path / "p", name)
    ingest.receive(proj, root, date)
    _recognize(proj)
    return proj


@pytest.fixture
def twin(tmp_path):
    root, date = make_sample.build_towers(tmp_path / "收图")
    return _receive(tmp_path, root, date)


def test_same_number_in_two_towers(twin):
    st = state.build(twin)
    by = {s["no"]: s for s in st["sheets"]}
    assert {"T1 · 建施-01", "T2 · 建施-01", "公共 · 建施-90", "未分塔楼 · 建施-02"} <= set(by)
    assert "建施-01" not in by and st["towers"]["multi"] is True
    assert [t["name"] for t in st["towers"]["list"]] == ["T1", "T2", "公共"]
    t1 = by["T1 · 建施-01"]
    assert (t1["number"], t1["tower"], t1["fields"]["no"]["value"]) == ("建施-01", "T1", "建施-01")
    assert (t1["fields"]["tower"]["value"], t1["fields"]["tower"]["src"]) == ("T1", "文件夹“T1”")
    assert len(t1["versions"]) == 1 and t1["status"] != "conflict"  # 两栋的建施-01 不混成一张，不误报版本冲突
    assert not [i for i in st["issues"] if i["kind"] == "版本冲突"]


def test_missing_checked_per_tower(twin):
    st = state.build(twin)
    missing = {i["target"]["sheet"] for i in st["issues"] if i["kind"] == "缺图"}
    assert missing == {"T1 · 建施-03", "T1 · 建施-04"}  # 只有 T2 有：T1 的目录列了、T1 的平面引用了，T1 缺
    edges = {(e["from"], e["to"]) for e in st["edges"]}
    assert ("T1 · 建施-02", "公共 · 建施-90") in edges  # 引用公共的图找得到
    assert ("T2 · 建施-01", "T2 · 建施-04") in edges and ("T1 · 建施-01", "T1 · 建施-04") in edges
    rows = {r["group"] for r in st["floor_coverage"]}
    assert any(g.startswith("T1 ") for g in rows) and any(g.startswith("T2 ") for g in rows)


def test_unknown_tower_file_asks(twin):
    st = state.build(twin)
    iss = next(i for i in st["issues"] if i["kind"] == "塔楼不明")
    assert iss["target"] == {"sheet": "未分塔楼 · 建施-02"} and "建施-02 二层平面图（补）.pdf" in iss["title"]
    assert [o["value"] for o in iss["options"]] == ["T1", "T2", towers.COMMON]
    add_decision(twin, "file_tower", iss["options"][0]["key"], "T2")
    st = state.build(twin)
    by = {s["no"]: s for s in st["sheets"]}
    assert "未分塔楼 · 建施-02" not in by and not [i for i in st["issues"] if i["kind"] == "塔楼不明"]
    assert len(by["T2 · 建施-02"]["versions"]) == 2  # 补图归到 T2，跟 T2 的建施-02 比版本
    assert by["T2 · 建施-02"]["status"] == "conflict"  # 同为 A 版但内容不同：改图没升版


def test_same_floor_within_tower(twin):
    st = state.build(twin)
    near = {r["no"] for r in state.same_floor(st, "T1 · 建施-01")["same_floor"]}
    assert "T1 · 结施-01" in near and "T2 · 结施-01" not in near


def test_assistant_asks_which_tower(twin):
    kit = Toolkit(twin)
    with pytest.raises(ToolError, match="T1 · 建施-01"):
        kit.get_sheet("建施-01")
    s = kit.get_sheet("T1 · 建施-01")
    assert s["no"] == "T1 · 建施-01" and s["tower"] == "T1"
    assert kit.get_sheet("建施-90")["no"] == "公共 · 建施-90"  # 只有一张的，光写图号也找得到


def _pdf(no, title, rev="A", variant=0):
    return make_sample.sheet_pdf(no, title, rev, "2026-09-01", [], [(rev, "2026-09-01", "出图")], variant=variant)


def test_decisions_carried_over_when_second_tower_arrives(tmp_path):
    """一栋楼时按图号做的决定（以哪版为现行），第二栋楼的图来了以后照样有效。"""
    first = tmp_path / "第一批"
    (first / "T1").mkdir(parents=True)
    (first / "T1" / "建施-01 一层平面图.pdf").write_bytes(_pdf("建施-01", "一层平面图"))
    proj = _receive(tmp_path, first, "2026-09-01")
    assert [s["no"] for s in state.build(proj)["sheets"]] == ["建施-01"]  # 一栋楼：身份就是图号
    add_decision(proj, "sheet_current", "建施-01", "A")
    second = tmp_path / "第二批"
    (second / "T2").mkdir(parents=True)
    (second / "T2" / "建施-01 一层平面图.pdf").write_bytes(_pdf("建施-01", "一层平面图", variant=1))
    _receive(tmp_path, second, "2026-09-10", proj=proj)
    by = {s["no"]: s for s in state.build(proj)["sheets"]}
    assert set(by) == {"T1 · 建施-01", "T2 · 建施-01"}
    assert by["T1 · 建施-01"]["fields"]["rev"]["conf"] == "confirmed"  # 光图号做的决定带过来了
    diff = state.build(proj)["diff"]
    assert "T1 · 建施-01" not in diff["added"] and "T2 · 建施-01" in diff["added"]


def test_tower_alias_question(tmp_path):
    root = tmp_path / "收图"
    for rel, (no, title) in {"T1/建施-01 一层平面图.pdf": ("建施-01", "一层平面图"),
                             "1#楼/建施-02 二层平面图.pdf": ("建施-02", "二层平面图")}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(_pdf(no, title))
    proj = _receive(tmp_path, root, "2026-09-01")
    st = state.build(proj)
    iss = next(i for i in st["issues"] if i["kind"] == "塔楼叫法")
    assert iss["title"] == "T1 和 1#楼 是同一栋楼吗？" and {s["no"] for s in st["sheets"]} == {"T1 · 建施-01", "1#楼 · 建施-02"}
    add_decision(proj, "tower_alias", "1#楼", "T1")
    st = state.build(proj)
    assert not [i for i in st["issues"] if i["kind"] == "塔楼叫法"]
    assert {(s["no"], s["tower"]) for s in st["sheets"]} == {("建施-01", "T1"), ("建施-02", "T1")}  # 并成一栋，身份回到图号


def test_iso_volume_is_the_tower(tmp_path):
    root = tmp_path / "收图"
    root.mkdir()
    for no in ("PRJ1-ARC-T1-01-DR-A-1001", "PRJ1-ARC-T2-01-DR-A-1001"):
        (root / f"{no} Level 01 GA Plan.pdf").write_bytes(_pdf(no, "Level 01 GA Plan"))
    st = state.build(_receive(tmp_path, root, "2026-09-01"))
    assert {(s["no"], s["tower"]) for s in st["sheets"]} == {("PRJ1-ARC-T1-01-DR-A-1001", "T1"), ("PRJ1-ARC-T2-01-DR-A-1001", "T2")}
    assert st["sheets"][0]["fields"]["tower"]["src"] == "图号里的分区代码"
