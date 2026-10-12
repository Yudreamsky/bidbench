import pytest

from bidbench import reports, state
from bidbench.floors import ROOF, cn_int, compress, parse_floors
from bidbench.store import add_decision


@pytest.mark.parametrize("text,floors", [
    ("一层平面图", {1}),
    ("首层平面图", {1}),
    ("二十一层平面图", {21}),
    ("三层梁配筋图", {3}),
    ("地下一层平面图", {-1}),
    ("地下二层~地下一层平面图", {-2, -1}),
    ("B1层平面图", {-1}),
    ("-2F平面图", {-2}),
    ("屋顶平面图", {ROOF}),
    ("机房层平面图", {ROOF}),
    ("3~20层平面图", set(range(3, 21))),
    ("三~二十层平面图", set(range(3, 21))),
    ("标准层平面图（3F～20F）", set(range(3, 21))),
    ("适用楼层：4F~20F", set(range(4, 21))),
    ("5F、6F 平面图", {5, 6}),
    ("标准层平面图", set()),
    ("1-1剖面图", set()),
    ("比例 1:100", set()),
    ("B版", set()),
    ("屋面标高 74.400", set()),
])
def test_parse_floors(text, floors):
    assert parse_floors(text) == floors


def test_cn_int_and_compress():
    assert [cn_int(x) for x in ("十", "十二", "二十", "二十一", "九十九", "7")] == [10, 12, 20, 21, 99, 7]
    assert cn_int("百") is None
    assert compress([-2, -1, 1, 2, 4, 5, 6, 7, ROOF]) == "B2、B1、1F、2F、4F~7F、屋顶"
    assert compress([3, 4]) == "3F、4F"


def coverage(st):
    return {r["group"]: r for r in st["floor_coverage"]}


def test_floor_coverage_on_sample(demo):
    st = state.build(demo)
    fc = coverage(st)
    arch = fc["建筑平面图"]
    assert arch["missing"] == ["B2", "B1", "3F"]           # 地下 2 层没有平面图；标准层只适用 4F~20F
    assert {c["floors"] for c in arch["covered"]} == {"1F", "2F", "4F~20F", "屋顶"}
    assert "冲突" in arch["basis"]                         # 地上层数 20 / 21 尚未确认
    assert fc["结构梁配筋图"]["status"] == "ok"             # 3F 与 4F~20F 连续
    assert fc["电气平面图"]["status"] == "unchecked"
    issue = next(i for i in st["issues"] if i["id"] == "floor:建筑平面图")
    assert issue["kind"] == "楼层覆盖" and "B2、B1、3F" in issue["title"] and len(issue["options"]) == 2
    assert "楼层覆盖" in reports.missing_report(st)
    assert state.build(demo, 1)["diff"]["floor_gaps"] == ["建筑平面图缺 B2、B1、3F"]
    assert st["diff"]["floor_gaps"] == []                  # 第 4 批没有带来新的缺层


def test_floor_decisions(demo_copy):
    p = demo_copy
    issue = next(i for i in state.build(p)["issues"] if i["id"] == "floor:建筑平面图")
    ask = issue["options"][0]
    add_decision(p, ask["kind"], ask["key"], ask["value"])
    st = state.build(p)
    assert coverage(st)["建筑平面图"]["confirmed"] is True
    assert "建筑平面图缺少 B2、B1、3F" in reports.qa_report(st)

    no = issue["options"][1]
    add_decision(p, no["kind"], no["key"], no["value"])
    st = state.build(p)
    assert coverage(st)["建筑平面图"]["status"] == "accepted"
    assert not any(i["id"] == "floor:建筑平面图" for i in st["issues"])

    # 缺口变了（屋顶平面图作废、层数确认为 21），之前的“不需要”不再适用，重新提示
    add_decision(p, "sheet_void", "建施-05", "voided")
    add_decision(p, "fact", "地上层数", "21 层")
    st = state.build(p)
    arch = coverage(st)["建筑平面图"]
    assert arch["status"] == "missing" and arch["missing"] == ["B2", "B1", "3F", "21F", "屋顶"]
    assert "人工确认" in arch["basis"]
