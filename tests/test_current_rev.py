"""现行版（docs/图纸字段.md 9.2）：先比版次；版次一样、写法不同或没写，再比日期；收图先后不算；拿不准的问人。"""

import make_sample
import pytest
from conftest import _recognize

from bidbench import ingest, state
from bidbench.store import Project, add_decision


def pdf(rev, date, history=None, variant=0, no="建施-02", title="一层平面图"):
    hist = history if history is not None else ([(rev, date, "出图")] if rev else [])
    return make_sample.sheet_pdf(no, title, rev, date, [], hist, variant=variant)


@pytest.fixture
def batches(tmp_path):
    """按批次收图：batches([{文件名: PDF}, ...]) → 项目。"""
    def run(*bs):
        proj = Project.init(tmp_path / "p", "现行版")
        for i, files in enumerate(bs, 1):
            folder = tmp_path / f"批次{i}"
            folder.mkdir()
            for fn, data in files.items():
                (folder / fn).write_bytes(data)
            ingest.receive(proj, folder, f"2026-0{i}-01")
            _recognize(proj)
        return proj
    return run


def _sheet(proj, no="建施-02"):
    st = state.build(proj)
    return st, next(s for s in st["sheets"] if s["no"] == no)


def _kinds(st):
    return {i["id"].split(":")[0]: i for i in st["issues"]}


@pytest.mark.parametrize("text,iso", [
    ("2026-09-01", "2026-09-01"), ("2026/9/1", "2026-09-01"), ("2026年9月1日", "2026-09-01"), ("2026.09.01", "2026-09-01"),
    ("12 Sep 2026", "2026-09-12"), ("12-Sep-26", "2026-09-12"), ("13/09/2026", "2026-09-13"), ("20260901", "2026-09-01"),
    ("12/09/2026", None),  # 分不清日、月
    ("-", None), (None, None),
])
def test_date_iso(text, iso):
    assert state._date_iso(text) == iso


def test_higher_rev_wins_regardless_of_batch(batches):
    """后收到的反而是旧版：按版次定现行，只提醒核实（不算冲突）。"""
    proj = batches({"建施-02 B.pdf": pdf("B", "2026-03-01")}, {"建施-02 A.pdf": pdf("A", "2026-01-01", variant=1)})
    st, s = _sheet(proj)
    assert s["current_rev"] == "B" and s["status"] != "conflict"
    iss = _kinds(st)["rev_older"]
    assert iss["level"] == "warn" and "后收到的反而是旧版" in iss["title"]
    add_decision(proj, "sheet_current", "建施-02", "B")  # 核实过：B 是现行版
    st, s = _sheet(proj)
    assert "rev_older" not in _kinds(st) and s["fields"]["rev"]["conf"] == "confirmed"


def test_same_rev_later_date_wins(batches):
    proj = batches({"建施-02 A.pdf": pdf("A", "2026-01-01")}, {"建施-02 A 改.pdf": pdf("A", "2026-02-15", variant=1)})
    st, s = _sheet(proj)
    assert s["status"] != "conflict" and next(v for v in s["versions"] if v["state"] == "现行")["batch"] == 2
    iss = _kinds(st)["rev_same"]
    assert iss["kind"] == "改图未升版" and [o["kind"] for o in iss["options"]] == ["sheet_pick", "rev_check", "rev_check"]
    add_decision(proj, "rev_check", iss["options"][1]["key"], "ask")  # 列入答疑
    st, _ = _sheet(proj)
    assert _kinds(st)["rev_same"]["confirmed"] and _kinds(st)["rev_same"]["qa"]
    add_decision(proj, "rev_check", iss["options"][1]["key"], "ignore")
    assert "rev_same" not in _kinds(_sheet(proj)[0])


def test_same_rev_same_date_asks(batches):
    proj = batches({"建施-02 A.pdf": pdf("A", "2026-01-01")}, {"建施-02 A 改.pdf": pdf("A", "2026-01-01", variant=1)})
    st, s = _sheet(proj)
    assert s["status"] == "conflict" and _kinds(st)["rev_dup"]["level"] == "crit"


def test_rev_and_date_disagree_asks(batches):
    """版次高的日期反而早：先按版次，列成问题。"""
    proj = batches({"建施-02 B.pdf": pdf("B", "2026-01-01"), "建施-02 A.pdf": pdf("A", "2026-03-01", variant=1)})
    st, s = _sheet(proj)
    assert s["current_rev"] == "B" and s["status"] == "conflict"
    iss = _kinds(st)["rev_date"]
    assert "版次和日期对不上" in iss["title"] and [o["value"] for o in iss["options"]] == ["B", "A"]


def test_mixed_schemes_by_date(batches):
    """B 和 P02 写法不同，没法比版次：按日期；没有日期就问。"""
    proj = batches({"建施-02 B.pdf": pdf("B", "2026-03-01"), "建施-02 P02.pdf": pdf("P02", "2026-01-01", variant=1)})
    st, s = _sheet(proj)
    assert s["current_rev"] == "B" and "rev_unclear" not in _kinds(st)


def test_no_dates_and_no_rev_asks(batches):
    proj = batches({"建施-02 一层平面图.pdf": pdf("", "-", history=[]),
                    "建施-02 B.pdf": pdf("B", "-", history=[], variant=1)})
    st, s = _sheet(proj)
    iss = _kinds(st)["rev_unclear"]
    assert s["status"] == "conflict" and s["current_rev"] == "B"  # 先按写了版次的那份
    assert {o["value"] for o in iss["options"]} == {"B", "?"}


def test_revision_table_date_preferred(batches):
    """图签日期是第一次出图的日期，修改记录里 B 版那一行才是 B 版的日期。"""
    hist = [("A", "2026-01-01", "出图"), ("B", "2026-05-01", "修改")]
    proj = batches({"建施-02 B.pdf": pdf("B", "2026-01-01", history=hist),
                    "建施-02 A.pdf": pdf("A", "2026-02-01", history=[("A", "2026-02-01", "出图")], variant=1)})
    st, s = _sheet(proj)
    assert s["current_rev"] == "B" and "rev_date" not in _kinds(st)  # 用修改记录的日期，版次和日期对得上
    assert s["fields"]["date"]["value"] == "2026-05-01" and "修改记录" in s["fields"]["date"]["src"]
