"""楼层保留原叫法再换算；英文楼层怎么数，软件先猜、问人确认（docs/图纸字段.md 9.3）。"""

import make_sample
import pytest
from conftest import _recognize

from bidbench import floors, ingest, state
from bidbench.store import Project, add_decision


@pytest.mark.parametrize("text,names_,base,got", [
    ("Level 05 GA Plan", ["Level 05"], 1, {6}),
    ("Ground Floor GA Plan", ["Ground Floor"], 1, {1}),
    ("Levels 02-04 Typical Plan", ["Levels 02-04"], 0, {2, 3, 4}),
    ("Basement 01 Slab Layout", ["Basement 01"], 1, {-1}),
    ("Level 05M Mezzanine Plan", ["Level 05M"], 0, {5}),  # 夹层算在下面那一层
    ("4F~20F标准层平面图", ["4F~20F"], 1, set(range(4, 21))),
    ("地下一层平面图", ["地下一层"], 1, {-1}),
    ("五层平面图", ["五层"], 1, {5}),
    ("15F避难层平面图", ["15F"], 1, {15}),
    ("屋顶平面图", ["屋顶"], 1, {floors.ROOF}),
])
def test_floor_names(text, names_, base, got):
    assert floors.floor_names(text) == names_ and floors.parse_floors(text, base) == got


def _project(tmp_path, files: dict):
    root = tmp_path / "收图"
    for rel, (no, title) in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(make_sample.sheet_pdf(no, title, "A", "2026-09-01", [], [("A", "2026-09-01", "出图")]))
    proj = Project.init(tmp_path / "p", "楼层")
    ingest.receive(proj, root, "2026-09-01")
    _recognize(proj)
    return proj


def _by(st):
    return {s["no"]: s for s in st["sheets"]}


def test_level_names_and_base_question(tmp_path):
    proj = _project(tmp_path, {"建施-01 Ground Floor GA Plan.pdf": ("建施-01", "Ground Floor GA Plan"),
                               "建施-05 Level 05 GA Plan.pdf": ("建施-05", "Level 05 GA Plan")})
    st = state.build(proj)
    f = _by(st)["建施-05"]["fields"]["floors"]
    assert f["value"] == "Level 05（6F）" and f["conf"] == "mid" and "猜的" in f["src"]  # 有 Ground Floor：先按英式猜
    iss = next(i for i in st["issues"] if i["kind"] == "楼层数法")
    assert iss["id"] == "floor_base:*" and "Ground Floor" in iss["detail"]
    assert [o["value"] for o in iss["options"]] == ["1", "0"]  # 猜的那个排前面
    add_decision(proj, "floor_base", "*", "0")  # 确认：Level 01 是首层
    st = state.build(proj)
    f = _by(st)["建施-05"]["fields"]["floors"]
    assert f["value"] == "Level 05（5F）" and f["conf"] == "high" and "已确认" in f["src"]
    assert not [i for i in st["issues"] if i["kind"] == "楼层数法"]


def test_base_asked_per_tower_when_guesses_differ(tmp_path):
    proj = _project(tmp_path, {"T1/建施-01 Ground Floor Plan.pdf": ("建施-01", "Ground Floor Plan"),
                               "T1/建施-05 Level 05 Plan.pdf": ("建施-05", "Level 05 Plan"),
                               "T2/建施-01 Level 01 Plan.pdf": ("建施-01", "Level 01 Plan"),
                               "T2/建施-05 Level 05 Plan.pdf": ("建施-05", "Level 05 Plan")})
    st = state.build(proj)
    assert {i["id"] for i in st["issues"] if i["kind"] == "楼层数法"} == {"floor_base:T1", "floor_base:T2"}
    by = _by(st)
    assert by["T1 · 建施-05"]["floors"] == [6] and by["T2 · 建施-05"]["floors"] == [5]  # 各栋各按各的数法
    add_decision(proj, "floor_base", "T2", "1")
    assert _by(state.build(proj))["T2 · 建施-05"]["floors"] == [6]


def test_chinese_floors_need_no_question(tmp_path):
    proj = _project(tmp_path, {"建施-02 二层平面图.pdf": ("建施-02", "二层平面图")})
    st = state.build(proj)
    assert not [i for i in st["issues"] if i["kind"] == "楼层数法"]
    assert _by(st)["建施-02"]["floors_name"] == "二层（2F）"
