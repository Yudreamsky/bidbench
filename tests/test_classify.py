"""智能分类：AI 给图纸分专业、小类、起简称，给事实分多级目录。用假的 Codex 和假的 Claude 客户端，不联网、不占额度。"""

import functools
import json
import re
import threading
from pathlib import Path

import make_sample
import pytest
from test_claude_executor import FakeClient
from test_codex_executor import FakeCodex, _arg, _body_from_prompt, _fake_status
from test_desktop import Client

from bidbench import classify, cli, oplog, ops, recognition, server, state


def pdf(no, title, *body):
    return make_sample.sheet_pdf(no, title, "A", "2026-01-01", list(body), [("A", "2026-01-01", "出图")])


@pytest.fixture
def mixed(sheets_project):
    """中文图和英文图混在一起：英文图是 ISO 19650 编号，按图号认不出专业。"""
    return sheets_project({
        "建施-01 建筑设计总说明.pdf": pdf("建施-01", "建筑设计总说明", "地上 20 层，地下 2 层", "建筑高度 74.40 m",
                                       "执行《建筑设计防火规范》GB 50016-2014（2018年版）"),
        "建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图", "1F 层高 5.10 m", "2F 层高 4.50 m"),
        "结施-03 三层梁配筋图.pdf": pdf("结施-03", "三层梁配筋图", "KL1 截面 300×650", "KL3 截面 350×700", "Q1 墙厚 300"),
        "A-1001.pdf": pdf("CGK9-ARC-ZZ-00-DR-A-1001", "Ground Floor GA Plan"),
        "A-1002.pdf": pdf("CGK9-ARC-ZZ-01-DR-A-1002", "Level 01 GA Plan"),
        "S-2001.pdf": pdf("CGK9-STR-ZZ-00-DR-S-2001", "Ground Floor Slab Layout"),
    })


def _rows(prompt: str, head: str) -> list[list[str]]:
    return [ln.split("|") for ln in prompt.split(head, 1)[1].splitlines() if re.match(r"^\d+\|", ln)]


def ai(prompt, n):
    """像模型一样回答智能分类；识别页面的提示照旧交给规则执行器。中文图的专业故意答错：按图号认得出的不该改。"""
    if "下面每行是一张图" in prompt:
        out = []
        for i, no, title, _folder in _rows(prompt, "图纸：\n"):
            code = no.split("-")[1] if no.count("-") > 3 else ""
            english = bool(re.search("[A-Za-z]", title))
            out.append({"i": int(i), "discipline": {"ARC": "建筑", "STR": "结构"}.get(code, "暖通"),
                        "group": "平面图" if re.search("平面|Plan|Layout", title) else "说明" if "说明" in title else "配筋图",
                        "short": f"{title[:4]}简" if english else None})
        return {"sheets": out}, None
    if "下面每行是一类事实" in prompt:
        out = []
        for i, names, _value, _count in _rows(prompt, "事实：\n"):
            if names.startswith("〔规范〕"):
                out.append({"i": int(i), "top": "执行规范", "sub": ["国家标准"]})
            elif "层高" in names:
                out.append({"i": int(i), "top": "楼层", "sub": ["层高"]})
            elif "截面" in names:
                out.append({"i": int(i), "top": "结构构件", "sub": ["梁", "截面"]})
            elif "墙厚" in names:
                out.append({"i": int(i), "top": "结构构件", "sub": ["墙", "墙厚"]})
            else:
                out.append({"i": int(i), "top": "工程概况", "sub": []})
        return {"facts": out}, None
    return _body_from_prompt(prompt), None


def _facts(st):
    return {f["name"]: (g["id"], f["path"], f["ai"]) for g in st["facts"]["groups"] for f in g["facts"]}


@pytest.mark.parametrize("name,template,family", [
    ("KL3 截面（三层梁配筋图）", "KL#截面", "截面"),
    ("KL12 截面", "KL#截面", "截面"),
    ("3F~20F 层高", "#F~#F层高", "层高"),
    ("十二层层高", "#层层高", "层高"),
    ("混凝土强度等级（梁）", "混凝土强度等级（梁）", "混凝土强度等级"),
    ("C1521 樘数", "C#樘数", "樘数"),
    ("Level 3 SSL", "Level#SSL", "Level SSL"),
    ("地上层数", "地上层数", "地上层数"),
])
def test_template_and_family(name, template, family):
    assert classify.fact_template(name) == template
    assert classify.fact_family(name) == family


def test_rule_paths_before_any_ai(mixed):
    """还没智能分类：同一说法有两条以上的归成小类（1F、2F 层高 → 层高），单独一条的直接挂在大类下。"""
    facts = _facts(state.build(mixed))
    assert facts["1F 层高"] == ("楼层", ["层高"], False)
    assert facts["Q1 墙厚"] == ("结构构件", [], False)
    assert all(not ai for _g, _p, ai in facts.values())


def test_codex_classifies_sheets_and_facts(mixed):
    fake, schemas = FakeCodex(ai), []

    def runner(cmd, prompt, timeout):
        schemas.append(json.loads(Path(_arg(cmd, "--output-schema")).read_text(encoding="utf-8")))
        return fake(cmd, prompt, timeout)
    lines = []
    r = ops.classify(mixed, "codex", runner=runner, log=lines.append)
    assert (r["sheets"], r["facts"], r["missed"]) == (6, 7, 0), lines  # 9 条事实，7 类：1F/2F 层高、KL1/KL3 截面各算一类
    first = fake.calls[0]
    assert "-i" not in first["cmd"] and "--sandbox" in first["cmd"]  # 只发文字，不发图片
    assert "CGK9-ARC-ZZ-00-DR-A-1001|Ground Floor GA Plan|" in first["prompt"]
    assert all("工程名称" not in c["prompt"] for c in fake.calls)  # 不发页面的文字层

    def strict(n):  # OpenAI 严格模式：字段全部必填、不许多余字段、可空写成 anyOf
        if isinstance(n, dict):
            if isinstance(n.get("properties"), dict):
                assert n["required"] == list(n["properties"]) and n["additionalProperties"] is False
            assert not isinstance(n.get("type"), list), n
            for v in n.values():
                strict(v)
        elif isinstance(n, list):
            for v in n:
                strict(v)
    for s in schemas:
        strict(s)

    st = state.build(mixed)
    by = {s["no"]: s for s in st["sheets"]}
    assert by["CGK9-ARC-ZZ-00-DR-A-1001"]["discipline"] == "建筑"  # ISO 编号里的角色代码 A，按规则就认得出
    assert by["CGK9-ARC-ZZ-00-DR-A-1001"]["cls"] == {"group": "平面图", "short": "Grou简", "discipline": False}
    assert by["CGK9-STR-ZZ-00-DR-S-2001"]["discipline"] == "结构"
    assert by["建施-01"]["discipline"] == "建筑" and by["建施-01"]["cls"] == {"group": "说明", "short": None, "discipline": False}
    facts = _facts(st)
    assert facts["1F 层高"] == facts["2F 层高"] == ("楼层", ["层高"], True)
    assert facts["Q1 墙厚"] == ("结构构件", ["墙", "墙厚"], True)
    assert facts["建筑设计防火规范"] == ("执行规范", ["国家标准"], True)
    assert st["classification"]["sheets_done"] == 6 and st["classification"]["facts_done"] == 9
    assert st["classification"]["last"]["executor"] == "codex"

    n = len(fake.calls)  # 再点一次：都分过了，不再问
    assert ops.classify(mixed, "codex", runner=runner, log=lines.append)["sheets"] == 0 and len(fake.calls) == n
    assert ops.classify(mixed, "codex", runner=runner, redo=True, log=lines.append)["sheets"] == 6


def test_ai_discipline_only_when_rules_cannot_tell():
    lab = {"discipline": "暖通", "group": "平面图", "short": None}
    assert classify.sheet_discipline(lab, "其他") == "暖通"
    assert classify.sheet_view(lab, "其他") == {"group": "平面图", "short": None, "discipline": True}
    assert classify.sheet_discipline(lab, "建筑") == "建筑"  # 按图号认得出的不听 AI 的
    assert classify.sheet_view({"discipline": "园林"}, "其他") is None


def test_pending_only_new_or_renamed():
    st = {"sheets": [{"no": "A-01", "title": "一层平面图", "paths": ["批次#1 / 图纸 / 建筑 / A-01.pdf"]},
                     {"no": "A-02", "title": "二层平面图", "paths": []}],
          "facts": {"groups": [{"id": "楼层", "facts": [{"name": "1F 层高", "value": "5.1 m", "std": False},
                                                        {"name": "7F 层高", "value": "3.6 m", "std": False}]}]}}
    done = {"sheet": {"A-01": {"title": "一层平面图"}, "A-02": {"title": "旧图名"}},
            "fact": {"#F层高": {"top": "楼层", "sub": ["层高"]}}}
    sheets, facts = classify.pending(st, done)
    assert [s["no"] for s in sheets] == ["A-02"] and facts == []  # 7F 层高是新识别出的，同一类已经分过，直接套用
    sheets, facts = classify.pending(st, done, redo=True)
    assert sheets[0]["folder"] == "图纸 / 建筑" and facts[0]["names"] == ["1F 层高", "7F 层高"] and facts[0]["count"] == 2


def test_clean_drops_bad_rows():
    sheets = [{"title": "一层平面图"}, {"title": "二层平面图"}]
    got = classify.clean("sheet", {"sheets": [
        {"i": 1, "discipline": "园林", "group": " 平面图 / ", "short": "一层平面图很长很长很长"},
        {"i": 1, "discipline": "建筑", "group": "重复的一行", "short": None},
        {"i": 9, "discipline": "建筑", "group": "越界", "short": None},
        {"i": 2, "discipline": "建筑", "group": "", "short": None}]}, sheets)
    assert got == {1: {"discipline": "其他", "group": "平面图", "short": "一层平面图很长很长很", "title": "一层平面图"}}
    facts = [{"std": False}, {"std": True}]
    got = classify.clean("fact", {"facts": [{"i": 1, "top": "执行规范", "sub": []},  # 不是规范却放进执行规范：不用
                                            {"i": 2, "top": "其他", "sub": ["执行规范", "图集", "第三级"]}]}, facts)
    assert got == {2: {"top": "执行规范", "sub": ["图集"]}}  # 规范一律放执行规范；小类最多两级，和大类同名的去掉
    assert classify.clean("fact", "不是对象", facts) == {}


def test_missed_items_stay_on_rules(mixed):
    lazy = FakeCodex(lambda p, n: ({"sheets": []} if "下面每行是一张图" in p else {"facts": []}, None))
    r = ops.classify(mixed, "codex", runner=lazy, log=lambda s: None)
    assert r["missed"] == 13 and r["sheets"] == r["facts"] == 0
    st = state.build(mixed)
    assert st["classification"]["sheets_done"] == 0 and _facts(st)["1F 层高"] == ("楼层", ["层高"], False)
    assert classify.pending(st, {"sheet": {}, "fact": {}})[0]  # 下次还会再分


def test_quota_stops_but_keeps_done(mixed, monkeypatch):
    monkeypatch.setattr(classify, "SHEET_BATCH", 2)
    fake = FakeCodex(lambda p, n: ai(p, n) if n == 1 else (None, "You've hit your usage limit."))
    with pytest.raises(ops.OpError, match="额度.*再分一次"):
        ops.classify(mixed, "codex", runner=fake, log=lambda s: None)
    with mixed.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM classifications WHERE kind='sheet'").fetchone()[0] == 2


def test_claude_classifies(mixed):
    client = FakeClient(lambda params, n: (ai(params["messages"][0]["content"][0]["text"], n)[0], "end_turn"))
    lines = []
    r = ops.classify(mixed, "claude", client=client, log=lines.append)
    assert (r["sheets"], r["facts"], r["model"]) == (6, 7, classify.CLAUDE_MODEL)
    call = client.calls[0]
    assert call["system"][0]["text"] == classify.SYSTEM and call["output_config"]["format"]["type"] == "json_schema"
    assert '"type": [' not in json.dumps(call["output_config"]["format"]["schema"])
    assert any("约 $" in ln for ln in lines)


def test_workbench_job_and_after_recognition(mixed, monkeypatch):
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: _fake_status())
    fake = FakeCodex(ai)
    monkeypatch.setattr(ops, "recognize_codex", functools.partial(ops.recognize_codex, runner=fake))
    monkeypatch.setattr(ops, "classify", functools.partial(ops.classify, runner=fake))
    httpd = server.serve(mixed, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        assert c.post("/api/job", {"kind": "classify", "executor": "gpt"})["_status"] == 400
        j = c.job("classify", executor="simulated")
        assert j["status"] == "done" and j["title"] == "智能分类（演示）", j
        st = c.get("/api/state")
        assert st["classification"]["sheets_done"] == 6
        assert {s["no"]: s["discipline"] for s in st["sheets"]}["CGK9-STR-ZZ-00-DR-S-2001"] == "结构"  # ISO 编号里的角色代码
        # 识别完自动分一次：只分新出现的（这里都分过了），再全部重新分一次看 AI 的结果
        j = c.job("recognize", executor="codex", scope="all", priorities=["catalog", "notes", "plans", "sections",
                                                                          "details", "other"])
        assert j["status"] == "done" and any("智能分类" in ln for ln in j["lines"]), j
        j = c.job("classify", executor="codex", redo=True)
        assert j["status"] == "done" and j["title"] == "智能分类（Codex，全部重新分）", j
        st = c.get("/api/state")
        assert {s["no"]: s["cls"] for s in st["sheets"]}["CGK9-ARC-ZZ-01-DR-A-1002"]["short"] == "Leve简"
    finally:
        httpd.shutdown()
    assert [e["title"] for e in oplog.read(mixed)][:3] == ["智能分类（Codex，全部重新分）", "Codex 识别（经济档）", "智能分类（演示）"]


def test_classify_along_recognition(mixed, monkeypatch):
    """边识别边分类：途中每导入一批就在后台分一次，识别完再把剩下的分完。"""
    monkeypatch.setattr(ops, "LIVE_IMPORT_PAGES", 1)
    monkeypatch.setattr(ops, "CLASSIFY_EVERY", 1)
    fake = FakeCodex(ai)
    monkeypatch.setattr(ops, "classify", functools.partial(ops.classify, runner=fake))
    task = recognition.make_task(mixed, scope="all", render=False)
    lines = []
    along = ops.ClassifyAlong(mixed, "codex", None, lines.append)
    ops.recognize_codex(mixed, task, runner=fake, workers=1, log=lambda s: None, on_import=along)
    along.finish()
    assert any("边识别边分类" in ln for ln in lines), lines
    assert state.build(mixed)["classification"]["sheets_done"] == 6
    assert not ops.ClassifyAlong(mixed, "simulated").on  # 模拟识别不分类


def test_cli_classify(mixed, capsys):
    cli.main(["-p", str(mixed.root), "classify", "--executor", "simulated"])
    out = capsys.readouterr().out
    assert "按规则演示" in out and "分好 6 张图" in out
    assert oplog.read(mixed)[0]["title"] == "智能分类（演示）"
