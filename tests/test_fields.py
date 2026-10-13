"""每个字段统一带值、原文、来源、把握、出处（docs/图纸字段.md 第 3 节）。"""

import json

import make_sample
import pytest

from bidbench import state
from bidbench.assistant import Toolkit
from bidbench.store import add_decision, now_iso


def pdf(no, title, rev="A"):
    return make_sample.sheet_pdf(no, title, rev, "2026-01-01", [], [(rev, "2026-01-01", "出图")])


def _pick(f, *keys):
    return tuple(f[k] for k in keys)


@pytest.mark.parametrize("score,conf", [(0.95, "high"), (0.85, "high"), (0.84, "mid"), (0.6, "mid"), (0.59, "low"),
                                        (None, None), (True, None)])
def test_score_to_conf(score, conf):
    assert state.conf_of(score) == conf


def _set_score(proj, name_part, key, score):
    """把一个文件识别结果里某一项的分数改掉，模拟识别没把握。"""
    with proj.connect() as con:
        rows = con.execute("SELECT r.page_id, r.data FROM recognitions r JOIN pages p ON p.page_id = r.page_id"
                           " JOIN occurrences o ON o.sha256 = p.file_sha256 WHERE o.path LIKE ?", (f"%{name_part}%",)).fetchall()
        for r in rows:
            data = json.loads(r["data"])
            data["title_block"][key]["confidence"] = score
            con.execute("UPDATE recognitions SET data=? WHERE page_id=?", (json.dumps(data, ensure_ascii=False), r["page_id"]))


def test_recognized_sheet_fields(sheets_project):
    proj = sheets_project({"建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图"),
                           "PRJ1-ARC-ZZ-05-DR-A-1006 Level 05 GA Plan.pdf": pdf("PRJ1-ARC-ZZ-05-DR-A-1006", "Level 05 GA Plan")})
    by = {s["no"]: s for s in state.build(proj)["sheets"]}
    f = by["建施-02"]["fields"]
    assert {k: _pick(f[k], "value", "conf") for k in ("no", "title", "rev", "date")} == {
        "no": ("建施-02", "high"), "title": ("一层平面图", "high"), "rev": ("A", "high"), "date": ("2026-01-01", "high")}
    assert f["title"]["src"] == "看图识别（PDF 文字层）" and f["title"]["where"]["page_id"] == by["建施-02"]["current_page"]
    assert _pick(f["discipline"], "value", "src", "conf") == ("建筑", "图号", "high")
    assert _pick(f["floors"], "value", "src", "conf") == ("1F", "图名（看图识别）", "high")
    assert _pick(f["content"], "src", "conf") == ("看图识别", "mid")
    iso = by["PRJ1-ARC-ZZ-05-DR-A-1006"]["fields"]
    assert iso["discipline"]["src"] == "图号里的角色代码"
    assert iso["floors"]["conf"] == "mid"  # Level 05 是几层，要看全项目按英式还是美式数
    _set_score(proj, "建施-02", "drawing_title", 0.5)
    f = {s["no"]: s for s in state.build(proj)["sheets"]}["建施-02"]["fields"]
    assert f["title"]["conf"] == "low" and f["floors"]["conf"] == "low"  # 楼层是从图名推的，跟着图名的把握


def test_raw_text_kept_when_different(sheets_project):
    proj = sheets_project({"建施05 立面图.pdf": pdf("建施05", "立面图")})
    f = state.build(proj)["sheets"][0]["fields"]
    assert _pick(f["no"], "value", "raw") == ("建施-05", "建施05")  # 图号整理过，原样写法留在原文里
    assert f["title"]["raw"] is None


def test_rev_confirmed_by_hand(sheets_project):
    proj = sheets_project({"建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图")})
    add_decision(proj, "sheet_current", "建施-02", "A")
    f = state.build(proj)["sheets"][0]["fields"]
    assert f["rev"]["conf"] == "confirmed" and f["rev"]["src"].startswith("人工确认")


def test_ai_guesses_are_low(sheets_project):
    """智能分类是 AI 看名字推的：专业、内容一律算把握低；看图识别读出的内容优先。"""
    proj = sheets_project({"X-11 Plant Room.pdf": pdf("X-11", "Plant Room"),
                           "Y-12 Pump Room.pdf": pdf("Y-12", "Pump Room")})
    label = json.dumps({"group": "平面图", "short": "机房", "discipline": "暖通", "content": "机房设备布置"}, ensure_ascii=False)
    with proj.connect() as con:
        con.execute("DELETE FROM recognitions WHERE page_id IN (SELECT p.page_id FROM pages p JOIN occurrences o"
                    " ON o.sha256 = p.file_sha256 WHERE o.path LIKE '%Y-12%')")  # 这张还没看图识别
        for key in ("X-11", "Y-12"):
            con.execute("INSERT INTO classifications(kind, key, label, executor, model, created_at)"
                        " VALUES ('sheet', ?, ?, 'codex', 'test', ?)", (key, label, now_iso()))
    by = {s["no"]: s for s in state.build(proj)["sheets"]}
    x, y = by["X-11"]["fields"], by["Y-12"]["fields"]
    assert _pick(x["discipline"], "value", "src", "conf") == ("暖通", state.AI_SRC, "low")
    assert x["content"]["src"] == "看图识别"
    assert by["Y-12"]["status"] == "guess"
    assert _pick(y["discipline"], "value", "conf") == ("暖通", "low")
    assert _pick(y["content"], "value", "src", "conf") == ("机房设备布置", state.AI_SRC, "low")


def test_assistant_gets_fields(sheets_project):
    proj = sheets_project({"建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图")})
    s = Toolkit(proj).get_sheet("建施-02")
    assert s["fields"]["rev"] == {"value": "A", "confidence": "高置信", "source": "看图识别（PDF 文字层）"}
    assert "guess_note" not in s
