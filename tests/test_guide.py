"""指引（bidbench/guide.py）：问题中心最上面的“下一步”、每个问题的“怎么选？”、做过的决定和撤回。"""

import ast
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bidbench import cli, guide, oplog, ops, reports, state
from bidbench.assistant import Toolkit
from bidbench.server import serve
from bidbench.store import Project, add_decision, latest_decisions, revoke_decision


def _issue_prefixes() -> set:
    """state.py 里各处 _issue(...) 的问题编号前缀（冒号前那段）。"""
    out = set()
    for node in ast.walk(ast.parse(Path(state.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_issue":
            iid = node.args[2]
            first = iid.values[0] if isinstance(iid, ast.JoinedStr) else iid
            assert isinstance(first, ast.Constant), ast.dump(iid)
            out.add(str(first.value).split(":")[0])
    return out


def test_every_issue_kind_has_help():
    """加了新问题就要写“怎么选？”；说明表里也不留已经没有的问题。"""
    prefixes = _issue_prefixes()
    assert {"missing", "rev_dup", "fact", "risk", "numbering", "floor_base", "extract"} <= prefixes  # 扫描本身没漏
    assert prefixes - set(guide.HELP) == set(), "这些问题还没写“怎么选？”"
    assert set(guide.HELP) - prefixes == set(), "这些说明对应的问题已经没有了"
    for k, h in guide.HELP.items():
        assert h["what"] and h["look"] and h["effect"], k


def _st(issues=(), batches=1, pages=10, recognized=10, proposals=(), numbering=None):
    return {"batches": [{"seq": n} for n in range(1, batches + 1)], "issues": list(issues),
            "recognition": {"pages": pages, "recognized": recognized}, "proposals": list(proposals),
            "numbering": numbering or {}, "security": {"pending": 0}}


def _i(iid, kind, level="warn", confirmed=False, opts=True):
    return {"id": iid, "kind": kind, "level": level, "confirmed": confirmed, "options": [{"label": "选这个"}] if opts else []}


def test_next_steps_order():
    """收图 → 认写法 → 看图识别 → 处理问题 → 导出；风险文件最先。"""
    st = _st([_i("missing:建施-09", "缺图", "crit"), _i("void:建施-05", "作废还是漏发"), _i("risk:abc", "风险文件", "crit"),
              _i("numbering", "图号格式"), _i("floor_base:*", "楼层数法"), _i("tower:123", "塔楼不明"),
              _i("extract:1", "解包问题", opts=False), _i("baseline:3层", "基线过期", opts=False),
              _i("unrecognized", "待识别", "info", opts=False),
              _i("missing:建施-10", "缺图", "crit", confirmed=True), _i("rev_same:建施-02", "改图未升版", confirmed=True)],
             recognized=4, proposals=[{"id": 3, "kind": "decision"}, {"id": 4, "kind": "numbering"}],
             numbering={"status": "proposed"})
    g = guide.next_steps(st)
    assert [t["id"] for t in g["tasks"]] == ["risk", "extract", "numbering", "tower", "floor_base", "recognize",
                                             "proposals", "crit", "warn", "notes", "export"]
    t = {x["id"]: x for x in g["tasks"]}
    assert t["risk"]["first"] == "risk:abc" and not t["risk"]["skip"] and t["tower"]["first"] == "tower:123"
    assert t["numbering"]["title"] == "核对 AI 助手认的图号格式"
    assert t["recognize"]["title"] == "看图识别：还有 6 页" and t["recognize"]["first"] is None
    assert "已识别 4 页" in t["recognize"]["text"] and "涉密" in t["recognize"]["text"]
    assert t["proposals"]["title"] == "核对 AI 助手的 1 条提议"  # 图号格式的提议在“认写法”里
    assert t["crit"]["title"] == "处理 1 个严重问题" and t["crit"]["first"] == "missing:建施-09"  # 已列入答疑的不算
    assert t["warn"]["title"] == "处理 1 个提醒" and t["warn"]["text"].startswith("作废还是漏发 1。")
    assert t["notes"]["title"] == "看一下 1 条提醒" and t["notes"]["text"].startswith("基线过期 1。")
    assert [x for x in t if t[x]["ack"]] == ["extract", "notes"]  # 没什么可选的：按钮是“知道了”
    assert not t["export"]["skip"]
    assert [s["done"] for s in g["stages"]] == [False] * 5


def test_next_steps_new_and_finished_project():
    g = guide.next_steps(_st(batches=0, pages=0, recognized=0))
    assert [t["id"] for t in g["tasks"]] == ["receive"] and not any(s["done"] for s in g["stages"])
    g = guide.next_steps(_st([_i("numbering", "图号格式")], numbering={"status": "guess"}))
    assert g["tasks"][0]["title"] == "确认图号格式"
    g = guide.next_steps(_st([_i("missing:建施-10", "缺图", "crit", confirmed=True), _i("unrecognized", "待识别", "info", opts=False)]))
    assert [t["id"] for t in g["tasks"]] == ["export"]
    assert [s["done"] for s in g["stages"]] == [True, True, True, True, False]
    g = guide.next_steps(_st(recognized=3))  # 没识别完：后面的步骤都不打勾
    assert [s["done"] for s in g["stages"]] == [True, True, False, False, False]


def test_skip_comes_back_when_issues_change():
    """“先做别的”记的是牵涉哪些问题：问题变了，这一步重新出现。"""
    def sig(*ids):
        return guide.next_steps(_st([_i(x, "缺图", "crit") for x in ids]))["tasks"][0]["sig"]
    assert sig("missing:建施-09") == sig("missing:建施-09") != sig("missing:建施-09", "missing:建施-11")


def test_state_carries_guide_and_help(demo):
    st = state.build(demo)
    assert st["guide"]["tasks"][-1]["id"] == "export"
    assert [x["key"] for x in st["guide"]["stages"]] == ["receive", "setup", "recognize", "issues", "export"]
    assert set(st["issue_help"]) == {guide.help_key(i["id"]) for i in st["issues"]} & set(guide.HELP)
    assert st["decided"] == []
    ai = Toolkit(demo)  # AI 助手解释问题、说下一步，用的是同一份
    assert ai.project_overview()["next_steps"][0].startswith(st["guide"]["tasks"][0]["title"])
    rows = ai.list_issues()
    assert set(rows["how_to_choose"]) == set(st["issue_help"])


def test_build_prints_next_step(demo_copy):
    lines = []
    ops.build(demo_copy, log=lines.append)
    assert lines[-1] == "下一步：" + state.build(demo_copy)["guide"]["tasks"][0]["title"]


def test_revoke_brings_the_question_back(demo_copy):
    st = state.build(demo_copy)
    iss = next(i for i in st["issues"] if i["kind"] == "缺图")
    no = next(o for o in iss["options"] if o["value"] == "not_needed")
    add_decision(demo_copy, no["kind"], no["key"], no["value"])
    st = state.build(demo_copy)
    assert iss["id"] not in {i["id"] for i in st["issues"]}
    d = st["decided"][0]
    assert (d["kind"], d["key"], d["group"]) == (no["kind"], no["key"], "缺图") and d["text"] == f"缺图 {no['key']}：确认不需要"

    revoke_decision(demo_copy, no["kind"], no["key"])
    st = state.build(demo_copy)
    assert iss["id"] in {i["id"] for i in st["issues"]} and st["decided"] == []
    with demo_copy.connect() as con:
        assert (no["kind"], no["key"]) not in latest_decisions(con)

    add_decision(demo_copy, no["kind"], no["key"], "ask")  # 撤回以后重新选
    st = state.build(demo_copy)
    assert next(i for i in st["issues"] if i["id"] == iss["id"])["confirmed"]
    titles = [e["title"] for e in oplog.read(demo_copy)]
    assert f"撤回决定：缺图 {no['key']}：确认不需要" in titles and f"决定：缺图 {no['key']}：列入答疑，向招标方要图" in titles


def test_server_revoke(demo_copy):
    add_decision(demo_copy, "sheet_void", "建施-05", "voided")
    httpd = serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body, token=None):
        req = urllib.request.Request(base + "/api/decision/revoke", method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Bidbench-Token": token or httpd.app.token})
        return json.loads(urllib.request.urlopen(req).read())

    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"kind": "sheet_void", "key": "建施-05"}, token="expired-token")
        assert e.value.code == 403
        st = post({"kind": "sheet_void", "key": "建施-05"})
        assert next(s for s in st["sheets"] if s["no"] == "建施-05")["status"] == "pending_void"
        assert any(i["id"] == "void:建施-05" for i in st["issues"]) and st["decided"] == []
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"kind": "sheet_void", "key": "建施-05"})  # 已经撤回了
        assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"kind": "revoke", "key": "建施-05"})
        assert e.value.code == 400
    finally:
        httpd.shutdown()


def test_cli_revoke(demo_copy, capsys):
    root = str(demo_copy.root)
    with pytest.raises(SystemExit, match="没有做过决定"):
        cli.main(["-p", root, "revoke", "sheet_void", "建施-05"])
    cli.main(["-p", root, "decide", "sheet_void", "建施-05", "voided"])
    cli.main(["-p", root, "revoke", "sheet_void", "建施-05", "--note", "点错了"])
    assert "已撤回：建施-05 确认已作废" in capsys.readouterr().out
    with Project.open(demo_copy.root).connect() as con:
        assert ("sheet_void", "建施-05") not in latest_decisions(con)


def test_decision_text_names_files():
    names = {"0123456789ab": "图纸/T1 补充.pdf", "0123456789ab-p001": "批次#2 的 电施-01.pdf"}
    assert guide.decision_text("file_tower", "0123456789ab", "T2", names) == "图纸/T1 补充.pdf 是 T2 的"
    assert guide.decision_text("file_tower", "0123456789ab", "公共", names) == "图纸/T1 补充.pdf 是几栋共用的"
    assert guide.decision_text("sheet_pick", "电施-01|B", "0123456789ab-p001", names) == "电施-01 B 版采用 批次#2 的 电施-01.pdf"
    assert guide.decision_text("sheet_pick", "电施-01|B", "ffffffffffff-p001") == "电施-01 B 版采用页面 ffffffffffff-p001"
    assert guide.decision_text("floor_base", "*", "0") == "全项目 英文楼层怎么数：Level 01 是首层（1F）"
    assert guide.decision_text("tower_alias", "1#楼", "T1") == "1#楼 和 T1 是同一栋楼"
    assert guide.decision_text("rev_check", "建施-02|A", "ask") == "建施-02 A 版改了图没升版：列入答疑"
    assert guide.decision_text("numbering", "*", "off").startswith("图号格式：先不用")
    fmt = {"formats": [{"segments": ["project", "discipline", "number", "rev?"], "example": "PRJ1-AR-1001-00"}]}
    assert guide.decision_text("numbering", "*", json.dumps(fmt)) == "图号格式：项目-专业-流水号（-版次）"


def test_qa_report_lists_asked_same_rev(demo_copy):
    """“改图未升版”点了列入答疑，要写进答疑清单。"""
    st = state.build(demo_copy)
    iss = next(i for i in st["issues"] if i["kind"] == "改图未升版")
    ask = next(o for o in iss["options"] if o["kind"] == "rev_check" and o["value"] == "ask")
    assert iss["qa"] not in reports.qa_report(st)
    add_decision(demo_copy, ask["kind"], ask["key"], ask["value"])
    assert iss["qa"] in reports.qa_report(state.build(demo_copy))
