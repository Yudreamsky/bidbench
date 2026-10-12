import make_sample
import pymupdf

from bidbench import oplog, reports, state
from bidbench.assistant import Toolkit
from bidbench.recognition import weak_reason
from bidbench.store import add_decision


def pdf(no, title, *body):
    """一张 A 版合成图纸。"""
    return make_sample.sheet_pdf(no, title, "A", "2026-01-01", list(body), [("A", "2026-01-01", "出图")])


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


def test_number_gaps_only_within_a_series(sheets_project):
    """分段编号（1xx 平面、2xx 剖面、9xx 系统图）之间的空档不算断号；同一段里连续缺 3 张以内才提示。"""
    proj = sheets_project({f"建施-{n}.pdf": pdf(f"建施-{n}", f"图{n}") for n in (101, 102, 103, 105, 110, 201, 202, 203, 901)})
    assert missing(state.build(proj)) == {"建施-104"}  # 106~109 连缺 4 张、111~200、204~900 都不算


def test_same_floor_general_and_sheet_values(sheets_project, demo):
    """建模时找另一个专业的对应图：同层图纸按楼层对上，设计说明、没写楼层的柱平面是通用图；
    本层结构标高这类只对本图有效的值不跨图比较。"""
    proj = sheets_project({
        "建施-01.pdf": pdf("建施-01", "建筑设计总说明"),
        "建施-02.pdf": pdf("建施-02", "二层平面图"),
        "建施-03.pdf": pdf("建施-03", "屋顶平面图"),
        "结施-01.pdf": pdf("结施-01", "结构设计总说明"),
        "结施-02.pdf": pdf("结施-02", "柱平面布置图", "1F~4F 层高 3.90 m"),
        "结施-03.pdf": pdf("结施-03", "二层梁配筋图", "本层结构标高 4.450"),
        "结施-04.pdf": pdf("结施-04", "屋面梁配筋图", "本层结构标高 16.150"),
    })
    st = state.build(proj)
    assert not [i for i in st["issues"] if i["kind"] == "事实冲突"]  # 4.450 / 16.150 是两层各自的标高
    s = sheets(st)
    assert [(v["name"], v["value"]) for v in s["结施-03"]["own_values"]] == [("本层结构标高", "4.450")]
    assert not any(f["name"] == "本层结构标高" for g in st["facts"]["groups"] for f in g["facts"])
    assert s["结施-02"]["general"] and s["结施-02"]["floors_text"] == "1F~4F"  # 图名没写楼层的柱平面
    roof = state.same_floor(st, "建施-03")
    assert roof["floors"] == "屋顶" and [x["no"] for x in roof["same_floor"]] == ["结施-04"]
    assert [x["no"] for x in roof["general"]] == ["建施-01", "结施-01", "结施-02"]
    assert [x["no"] for x in state.same_floor(st, "建施-02")["same_floor"]] == ["结施-03"]
    card = Toolkit(proj).get_sheet("建施-03")
    assert [x["no"] for x in card["same_floor"]] == ["结施-04"] and len(card["general_sheets"]) == 3
    # 演示项目：标准层平面写着“适用楼层：4F~20F”，记为本图的值，同层图纸对上标准层梁配筋图
    demo_st = state.build(demo)
    assert ("适用楼层", "4F~20F") in [(v["name"], v["value"]) for v in sheets(demo_st)["建施-04"]["own_values"]]
    assert [x["no"] for x in state.same_floor(demo_st, "建施-04")["same_floor"]] == ["结施-05"]


def test_change_notice_marks_facts(sheets_project):
    """变更单改过的图：事实提示数值可能已变，图纸卡片能打开变更单原件。"""
    proj = sheets_project({
        "建施-07.pdf": pdf("建施-07", "墙身大样", "Q1 墙厚 200", "混凝土强度等级 C30"),
        "结施-01.pdf": pdf("结施-01", "结构设计总说明", "混凝土强度等级 C30"),
        "设计变更单-001.pdf": make_sample.sheet_pdf("设计变更单-001", "设计变更通知单", "A", "2026-01-05",
                                                ["1 修改建施-07：Q1 墙厚改为 250"], [("A", "2026-01-05", "变更")], "—"),
    })
    st = state.build(proj)
    facts = {f["name"]: f for g in st["facts"]["groups"] for f in g["facts"]}
    assert facts["Q1 墙厚"]["notices"] == ["设计变更单-001"] and "可能已被修改" in facts["Q1 墙厚"]["note"]
    assert facts["混凝土强度等级"]["notices"] == []  # 结施-01 没被改过，数值仍可信
    assert sheets(st)["建施-07"]["status"] == "changed"
    card = Toolkit(proj).get_sheet("建施-07")
    assert card["notices"][0]["id"] == "设计变更单-001" and card["notices"][0]["page_id"]
    assert next(f for f in card["facts"] if f["name"] == "Q1 墙厚")["changed_by"] == ["设计变更单-001"]


def test_weak_recognition_ignores_remarks(sheets_project):
    """“图签没有专业栏”“没写单位”只是备注，不算识别不完整；缺图号、字迹不清才算。"""
    def rec(kind, no, *issue_types):
        tb = {"drawing_no": {"value": no, "confidence": 0.9}, "drawing_title": {"value": "图", "confidence": 0.9},
              "revision": {"value": "A", "confidence": 0.9}}
        return {"page_kind": kind, "title_block": tb, "issues": [{"type": t, "description": "……"} for t in issue_types]}
    assert weak_reason(rec("图纸", "建施-01", "其他")) is None
    assert weak_reason(rec("图纸", "建施-01", "其他", "字迹不清")) == "字迹不清"
    assert weak_reason(rec("图纸", None)) == "缺图号"
    assert weak_reason(rec("变更单", None)) is None  # 变更单本来就可以没有图号
    scan = pymupdf.open()
    scan.new_page().draw_rect(pymupdf.Rect(50, 50, 200, 200))  # 没有文字层的扫描件
    proj = sheets_project({"建施-01.pdf": pdf("建施-01", "一层平面图"), "扫描.pdf": scan.tobytes()})
    weak = next(i for i in state.build(proj)["issues"] if i["kind"] == "识别不完整")
    assert weak["title"] == "1 页识别不完整" and "缺图号 1 页" in weak["detail"]
