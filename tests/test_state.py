from bidbench import oplog, reports, state
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
    assert [state._rev_scheme(r) for r in ("B", "第2版", "A1", "草图")] == ["letter", "number", "mixed", None]


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


def test_catalog_revisions(demo_copy):
    assert not [i for i in state.build(demo_copy, 1)["issues"] if i["kind"] == "目录版次"]  # 第 1 批目录和图纸一致
    st = state.build(demo_copy, 2)
    cat = {i["id"]: i for i in st["issues"] if i["kind"] == "目录版次"}
    assert set(cat) == {"catalog_newer:建施-00:建施-06", "catalog_behind:结施-00"}
    newer, behind = cat["catalog_newer:建施-00:建施-06"], cat["catalog_behind:结施-00"]
    assert newer["level"] == "crit" and newer["title"] == "建施-06 目录写 B 版，手上只有 A 版"
    assert newer["target"] == {"sheet": "建施-06"}
    assert behind["level"] == "warn" and "结施-01 目录 A 版、收到 B 版" in behind["detail"]
    qa = reports.qa_report(st)
    assert "列出 建施-06 为 B 版，但收到的是 A 版" in qa
    assert "提供更新后的图纸目录" not in qa  # 提醒类的，列入答疑后才进答疑清单
    assert "建施-06 目录写 B 版，手上只有 A 版" in reports.missing_report(st)

    # “知道了”只管当时那几张图；第 3 批结施-04 又升 C 版，重新提示
    ok = behind["options"][1]
    add_decision(demo_copy, ok["kind"], ok["key"], ok["value"])
    assert "catalog_behind:结施-00" not in {i["id"] for i in state.build(demo_copy, 2)["issues"]}
    b3 = next(i for i in state.build(demo_copy, 3)["issues"] if i["id"] == "catalog_behind:结施-00")
    assert "结施-04 目录 A 版、收到 C 版" in b3["detail"]
    add_decision(demo_copy, "catalog_rev", b3["options"][0]["key"], "ask")
    st = state.build(demo_copy, 3)
    assert next(i for i in st["issues"] if i["id"] == "catalog_behind:结施-00")["confirmed"]
    assert "提供更新后的图纸目录" in reports.qa_report(st)

    # 目录笔误：不再提示
    add_decision(demo_copy, "catalog_rev", "建施-06|B", "ignore")
    assert not any(i["id"].startswith("catalog_newer:") for i in state.build(demo_copy)["issues"])
    assert oplog.decision_text("catalog_rev", "建施-06|B", "ignore") == "建施-06 目录写 B 版：以收到的版本为准"
    assert oplog.decision_text("catalog_rev", b3["options"][0]["key"], "ask") == "结施-00 目录版次没跟上图纸：列入答疑"


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


def test_number_gaps_only_within_a_series(tmp_path):
    """分段编号（1xx 平面、2xx 剖面、9xx 系统图）之间的空档不算断号；同一段里连续缺 3 张以内才提示。"""
    import make_sample
    from bidbench import ingest, recognition
    from bidbench.executors import simulated
    from bidbench.store import Project

    folder = tmp_path / "图纸"
    folder.mkdir()
    for n in (101, 102, 103, 105, 110, 201, 202, 203, 901):
        (folder / f"建施-{n}.pdf").write_bytes(
            make_sample.sheet_pdf(f"建施-{n}", f"图{n}", "A", "2026-01-01", [], [("A", "2026-01-01", "出图")]))
    proj = Project.init(tmp_path / "p", "分段编号")
    ingest.receive(proj, folder, "2026-01-01")
    task = recognition.make_task(proj, render=False)
    simulated.run(task)
    assert not recognition.import_results(proj, task / "results").errors
    assert missing(state.build(proj)) == {"建施-104"}  # 106~109 连缺 4 张、111~200、204~900 都不算
