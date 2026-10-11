from bidbench import state
from bidbench.store import add_decision


def sheets(st):
    return {s["no"]: s for s in st["sheets"]}


def missing(st):
    return {s["no"] for s in st["sheets"] if s["status"] == "missing"}


def test_norm_no_and_rev_key():
    assert state.norm_no("建施05") == "建施-05"
    assert state.norm_no("建施 －5") == "建施-05"
    assert state.norm_no("js-5") == "JS-05"
    assert state.rev_key("A") < state.rev_key("B") < state.rev_key("C")
    assert state.rev_key("0") < state.rev_key("1")
    assert state.rev_key("第2版") == state.rev_key("2")
    assert state.rev_key("草图") is None


def test_batch1_missing_drawings(demo):
    st = state.build(demo, 1)
    assert missing(st) == {"建施-09", "建施-12", "结施-03"}
    assert st["batches"][0]["kinds"] == ["首次发图"]
    s = sheets(st)
    # 合订本里的两页各自成为一张图
    assert s["建施-06"]["current_rev"] == "A" and s["建施-07"]["current_rev"] == "A"
    assert any("（第 2 页）" in p for p in s["建施-07"]["paths"])


def test_batch2_full_reissue(demo):
    st = state.build(demo, 2)
    b = st["batches"][1]
    assert b["kinds"] == ["整套重发"] and b["confidence"] == "高"
    s = sheets(st)
    assert s["建施-05"]["status"] == "pending_void"
    assert s["结施-04"]["current_rev"] == "B" and s["结施-04"]["status"] == "updated"
    assert s["电施-01"]["status"] == "new"
    assert "建施-05" in st["diff"]["pending_void"]


def test_batch3_nested_archive_resolves_missing(demo):
    st = state.build(demo, 3)
    s = sheets(st)
    assert "结施-03" not in missing(st)
    assert s["结施-03"]["inner"] is True
    assert "结施-03" in st["diff"]["resolved_missing"]
    assert s["结施-04"]["current_rev"] == "C"
    bl = st["baselines"][0]
    assert bl["outdated"] == [{"sheet": "结施-04", "base_rev": "B", "current_rev": "C"}]
    assert set(st["batches"][2]["kinds"]) == {"部分修改", "补充图纸"}
    assert any(i["kind"] == "解包问题" for i in st["issues"])


def test_batch4_same_revision_different_content(demo):
    st = state.build(demo, 4)
    s = sheets(st)
    assert s["电施-01"]["status"] == "conflict"
    assert s["建施-08"]["status"] == "changed"  # 设计变更单-012 修改了它
    assert "电施-01" in st["diff"]["new_conflicts"]
    assert st["notices"][0]["affects"] == ["建施-08"]


def test_facts(demo):
    st = state.build(demo)
    facts = {f["key"]: f for g in st["facts"]["groups"] for f in g["facts"]}
    assert facts["地上层数"]["conf"] == "conflict"
    assert set(facts["地上层数"]["sources"]) == {"建施-01", "建施-07", "结施-01"}
    assert facts["建筑高度"]["conf"] == "high"
    assert facts["结构形式"]["conf"] == "high"  # “框架-剪力墙结构”与“框架剪力墙”视为同一值
    kl3 = facts["KL3截面（三层梁配筋图）"]
    assert kl3["value"].startswith("350×700") and "升版" in kl3["note"]
    assert any(not e["active"] and e["value"] == "300×600" for e in kl3["evidence"])
    assert facts["GB50016-2014"]["conf"] == "high"


def test_decisions(demo_copy):
    p = demo_copy
    add_decision(p, "fact", "地上层数", "20 层", "结施-01 计入了机房层")
    add_decision(p, "sheet_void", "建施-05", "voided")
    add_decision(p, "missing", "建施-12", "not_needed")
    st = state.build(p)
    s = sheets(st)
    facts = {f["key"]: f for g in st["facts"]["groups"] for f in g["facts"]}
    assert facts["地上层数"]["conf"] == "confirmed" and facts["地上层数"]["value"] == "20 层"
    assert s["建施-05"]["status"] == "voided"
    assert "建施-12" not in missing(st)
    # 选定电施-01 采用哪一份后，冲突解除
    issue = next(i for i in st["issues"] if i["id"] == "rev_dup:电施-01")
    opt = issue["options"][1]
    add_decision(p, opt["kind"], opt["key"], opt["value"])
    st = state.build(p)
    assert sheets(st)["电施-01"]["status"] == "current"
    assert sheets(st)["电施-01"]["current_page"] == opt["value"]
