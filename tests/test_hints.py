"""提示更准确：事实原文带统计口径、问题牵涉的图纸、批次差异写清哪张图改了数、工作台自动刷新。"""

import threading

from bidbench import reports, server, state
from bidbench.assistant import Toolkit


def test_fact_conflict_quotes_counting_basis(demo):
    st = state.build(demo)
    f = next(f for g in st["facts"]["groups"] for f in g["facts"] if f["key"] == "地上层数")
    assert next(e["text"] for e in f["evidence"] if e["sheet"] == "结施-01" and e["active"]) == "地上 21 层（含屋顶机房层）"
    issue = next(i for i in st["issues"] if i["id"] == "fact:地上层数")
    assert "21（结施-01，原文“地上 21 层（含屋顶机房层）”）" in issue["detail"] and "口径不同" in issue["detail"]
    assert issue["sheets"] == ["建施-01", "建施-07", "结施-01"]
    assert next(i for i in st["issues"] if i["id"] == "missing:建施-09")["sheets"] == ["建施-00", "建施-02"]  # 目录列出、建施-02 引用


def test_sheet_card_shows_own_value_and_related_issues(demo):
    kit = Toolkit(demo)
    f = next(x for x in kit.get_sheet("结施-01")["facts"] if x["key"] == "地上层数")
    assert f == {"key": "地上层数", "name": "地上层数", "this_sheet": "地上 21 层（含屋顶机房层）",
                 "project_value": "20层", "confidence": "冲突"}
    assert {i["id"]: i["relation"] for i in kit.get_sheet("建施-01")["issues"]}["fact:地上层数"] == "相关"
    assert {i["id"]: i["relation"] for i in kit.get_sheet("建施-02")["issues"]}["missing:建施-09"] == "相关"
    assert {i["id"]: i["relation"] for i in kit.get_sheet("建施-05")["issues"]}["void:建施-05"] == "本图"


def test_batch_diff_names_the_changed_source(demo):
    st = state.build(demo, 2)
    d = st["diff"]
    assert d["new_fact_conflicts"] == ["地上层数：20（建施-01、建施-07） / 21（结施-01）。结施-01 B 版改为 21（原 A 版为 20）"]
    assert "地上层数：20层（高置信）→ 20层（冲突）。结施-01 B 版改为 21（原 A 版为 20）" in d["fact_changes"]
    assert any(x.startswith("总建筑面积") and x.endswith("建施-01 B 版改为 45860（原 A 版为 45200）") for x in d["fact_changes"])
    assert "## 新出现的事实冲突" in reports.diff_report(st)
    assert state.build(demo, 3)["diff"]["new_fact_conflicts"] == []  # 冲突早就有了，不算新的


def test_workbench_refreshes_after_outside_changes(demo_copy):
    from test_desktop import Client
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        st = c.get("/api/state")
        assert st["db_version"] and not st["proposals"]
        assert c.get("/api/state/version") == {"version": st["db_version"], "busy": False}
        Toolkit(demo_copy).propose_note("缺图", "建施-09 列入答疑", "外部 AI 助手提的")  # 另一个程序改了项目数据库
        v = c.get("/api/state/version")["version"]
        assert v != st["db_version"]
        st2 = c.get("/api/state")
        assert st2["db_version"] == v and [p["kind"] for p in st2["proposals"]] == ["note"]
    finally:
        httpd.shutdown()
