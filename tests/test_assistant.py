"""AI 助手工具层与 MCP 服务：查询只读；改动只能提议，确认后才生效。"""

import asyncio
import json
import threading
import urllib.request

import pytest

from bidbench import state
from bidbench.assistant import CONFIRMED_ACTOR, Toolkit, ToolError
from bidbench.server import serve


@pytest.fixture
def kit(demo_copy):
    return Toolkit(demo_copy)


def test_overview_and_lists(kit):
    ov = kit.project_overview()
    assert ov["project"] == "示例综合体项目" and ov["as_of_batch"] == 4
    assert ov["sheets_by_status"]["缺失"] == 2 and ov["issues_by_level"]["严重"] >= 4
    assert any("建筑平面图" in x for x in ov["floor_coverage"])
    assert len(kit.list_batches()) == 4
    assert "升版" in kit.batch_diff(3)["report"]
    assert kit.compare_batches(1, 4)["resolved_missing"] == ["结施-03"]

    missing = kit.list_sheets(status="缺失")
    assert {s["no"] for s in missing["sheets"]} == {"建施-09", "建施-12"}
    assert kit.list_sheets(discipline="结构", keyword="梁")["total"] == 2


def test_sheet_page_search(kit):
    s = kit.get_sheet("结施04")  # 写法不规范也能找到
    assert s["no"] == "结施-04" and s["current_rev"] == "C"
    assert [v["state"] for v in s["versions"]] == ["已替代", "已替代", "现行"]
    assert any(f["name"].startswith("KL3") for f in s["facts"])
    page = kit.get_page(s["current_page_id"], include_text_layer=True)
    assert "结施-04" in page["detail"] and "KL3" in page["text_layer"]
    hits = kit.search("KL3")
    assert [(h["sheet"], h["rev"]) for h in hits["hits"]] == [("结施-04", "C")]  # 默认只搜现行版
    assert len(kit.search("KL3", current_only=False)["hits"]) == 3
    with pytest.raises(ToolError, match="没有图号"):
        kit.get_sheet("建施-99")
    with pytest.raises(ToolError, match="格式"):
        kit.get_page("../../etc/passwd")


def test_facts_issues_and_drafts(kit):
    f = kit.get_fact("地上层数")
    assert f["confidence"] == "冲突" and {e["sheet"] for e in f["evidence"] if e["current"]} == {"建施-01", "建施-07", "结施-01"}
    assert any(x["name"] == "地上层数" for x in kit.list_facts(only_problems=True)["facts"])
    issues = kit.list_issues(level="严重")["issues"]
    assert any(i["id"] == "fact:地上层数" for i in issues)
    assert "答疑问题清单" in kit.draft_qa()["draft"]
    assert kit.floor_coverage()[0]["missing"] == "B2、B1、3F"
    est = kit.estimate_recognition("conflict", "strong")
    assert est["pages"] > 0 and "bidbench recognize" in est["command"]


def test_proposal_does_nothing_until_confirmed(kit, demo_copy):
    r = kit.propose_issue_resolution("fact:地上层数", "1", "建施-01 B 版、建施-07 A 版均为 20 层；结施-01 B 版计入了机房层")
    assert r["status"] == "待用户确认" and "bidbench confirm" in r["how_to_confirm"]
    # 提议本身不改变任何结论
    assert kit.get_fact("地上层数")["confidence"] == "冲突"
    assert len(state.build(demo_copy)["proposals"]) == 1
    # 重复提议不会堆积
    assert kit.propose_issue_resolution("fact:地上层数", "采用 20", "同上")["proposal_id"] == r["proposal_id"]

    out = kit.confirm(r["proposal_id"])
    assert out["status"] == "confirmed"
    f = kit.get_fact("地上层数")
    assert f["confidence"] == "人工确认" and f["value"].startswith("20")
    with demo_copy.connect() as con:
        d = con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert d["actor"] == CONFIRMED_ACTOR and "机房层" in d["note"]
    with pytest.raises(ToolError, match="已经处理过"):
        kit.confirm(r["proposal_id"])


def test_proposal_validation_reject_and_stale(kit):
    with pytest.raises(ToolError, match="理由"):
        kit.propose_issue_resolution("missing:建施-09", "1", " ")
    with pytest.raises(ToolError, match="option 应为"):
        kit.propose_issue_resolution("missing:建施-09", "9", "试试")
    with pytest.raises(ToolError, match="没有问题"):
        kit.propose_issue_resolution("missing:不存在", "1", "试试")

    a = kit.propose_issue_resolution("void:建施-05", "确认已作废", "第 2 批整套重发的新目录已不再列出建施-05")
    b = kit.propose_issue_resolution("void:建施-05", "2", "也可能是漏发")
    assert kit.reject(b["proposal_id"], "先按作废处理")["status"] == "rejected"
    kit.confirm(a["proposal_id"])
    assert kit.get_sheet("建施-05")["status"] == "已作废"
    # 情况变了，旧提议确认时自动失效，不会误执行
    c = kit.propose_issue_resolution("missing:建施-12", "1", "建施-03 引用了它")
    kit.confirm(kit.propose_issue_resolution("missing:建施-12", "2", "是引用笔误")["proposal_id"])
    assert kit.confirm(c["proposal_id"])["status"] == "stale"
    statuses = {p["id"]: p["status"] for p in kit.list_proposals("all")}
    assert statuses[b["proposal_id"]] == "rejected" and statuses[c["proposal_id"]] == "stale"


def test_baseline_and_note_proposals(kit):
    with pytest.raises(ToolError, match="没有图号"):
        kit.propose_baseline("模型", "测试", ["结施-99"])
    b = kit.propose_baseline("4 层结构模型", "开始建模前锁定", ["结施-01", "结施-05"])
    assert kit.list_baselines()[0]["name"] == "3层结构模型"  # 还没生效
    kit.confirm(b["proposal_id"])
    assert any(x["name"] == "4 层结构模型" for x in kit.list_baselines())

    n = kit.propose_note("层数口径", "本项目机房层不计入地上层数", "与建设单位电话确认")
    assert kit.list_notes() == []
    kit.confirm(n["proposal_id"])
    notes = kit.list_notes()
    assert notes[0]["topic"] == "层数口径" and notes[0]["source"] == CONFIRMED_ACTOR
    assert kit.project_overview()["notes"] == ["层数口径：本项目机房层不计入地上层数"]


def test_mcp_server_tools(kit):
    pytest.importorskip("mcp")
    from bidbench.mcp_server import build_server

    server = build_server(kit)

    async def go():
        tools = {t.name: t for t in await server.list_tools()}
        assert {"project_overview", "get_sheet", "search", "propose_issue_resolution", "propose_note"} <= set(tools)
        assert not {"confirm", "reject"} & set(tools)  # 确认、驳回不对 AI 开放
        def read_only(name):
            a = tools[name].annotations  # SDK 1.x 字段名 readOnlyHint，2.x 为 read_only_hint
            return getattr(a, "readOnlyHint", None) if hasattr(a, "readOnlyHint") else getattr(a, "read_only_hint", None)
        assert read_only("get_sheet") is True and read_only("propose_note") is False
        r = await server.call_tool("get_sheet", {"no": "结施-04"})
        content = r.content if hasattr(r, "content") else r[0] if isinstance(r, tuple) else r
        text = content[0].text
        assert json.loads(text)["current_rev"] == "C"
        r = await server.call_tool("get_sheet", {"no": "建施-99"})
        content = r.content if hasattr(r, "content") else r[0] if isinstance(r, tuple) else r
        assert "没有图号" in content[0].text
    asyncio.run(go())


def test_workbench_confirms_proposal(kit, demo_copy):
    p = kit.propose_note("说明", "测试笔记", "测试")
    httpd = serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        st = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert [x["id"] for x in st["proposals"]] == [p["proposal_id"]]
        req = urllib.request.Request(base + "/api/proposal", method="POST",
                                     headers={"Content-Type": "application/json", "X-Bidbench-Token": httpd.app.token},
                                     data=json.dumps({"id": p["proposal_id"], "action": "confirm"}).encode())
        st = json.loads(urllib.request.urlopen(req).read())
        assert st["proposals"] == [] and st["notes"][0]["text"] == "测试笔记"
        assert "已保存项目笔记" in st["last_outcome"]
    finally:
        httpd.shutdown()
