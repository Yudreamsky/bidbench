"""智能收图：给一级目录，只列文件夹不读文件，判断收哪些子文件夹、分几批，按计划一批批收。全是合成文件夹和假的 Codex。"""

import json
import re
from pathlib import Path

import make_sample
import pytest
from test_codex_executor import FakeCodex

from bidbench import cli, ingest, oplog, survey
from bidbench.store import Project


def _pdf(no, title):
    return make_sample.sheet_pdf(no, title, "A", "2026-01-01", [], [("A", "2026-01-01", "出图")])


@pytest.fixture
def share(tmp_path):
    """像一个项目共享盘：施工图（建筑、结构，结构下面还有照片）、深化图、邮件、会议纪要。"""
    top = tmp_path / "OASIS"
    files = {
        "0. EMAIL/re 答复.msg": b"mail", "0. EMAIL/fw 转发.eml": b"mail",
        "1. FOR CONSTRUCTION/1.1 ARCHITECTURE/建施-01 一层平面图.pdf": _pdf("建施-01", "一层平面图"),
        "1. FOR CONSTRUCTION/1.1 ARCHITECTURE/建施-02 二层平面图.dwg": b"AC1032" + b"\0" * 32,
        "1. FOR CONSTRUCTION/1.2 STRUCTURE/结施-01 结构总说明.pdf": _pdf("结施-01", "结构总说明"),
        "1. FOR CONSTRUCTION/1.2 STRUCTURE/photos/现场.jpg": b"\xff\xd8\xff",
        "1. FOR CONSTRUCTION/目录.pdf": _pdf("建施-00", "图纸目录"),
        "2. SHOPDRAWING/深施-01 节点.pdf": _pdf("深施-01", "节点"),
        "8. MEETING - MOM/第 1 次例会.docx": b"PK\x03\x04 doc",
        "说明.txt": b"readme",
    }
    for rel, data in files.items():
        p = top / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return top


@pytest.fixture
def proj(tmp_path):
    return Project.init(tmp_path / "p", "智能收图测试")


def test_scan_counts_without_reading_files(share, monkeypatch):
    def no_open(*a, **kw):
        raise AssertionError("列文件夹时不该打开文件")
    monkeypatch.setattr("builtins.open", no_open)
    root = survey.scan(share)
    assert (root.files, root.kinds["pdf"], root.kinds["cad"], root.kinds["email"]) == (10, 4, 1, 2)
    fc = next(c for c in root.children if c.rel == "1. FOR CONSTRUCTION")
    assert (fc.files, fc.direct, fc.drawings) == (5, 1, 4)
    assert [c.rel for c in fc.children] == ["1. FOR CONSTRUCTION/1.1 ARCHITECTURE", "1. FOR CONSTRUCTION/1.2 STRUCTURE"]


def test_rules_take_drawing_folders_and_leave_mail_and_photos(share):
    root = survey.scan(share)
    include, exclude = survey.rule_pick(root)
    assert set(include) == {"1. FOR CONSTRUCTION", "2. SHOPDRAWING"}
    assert set(exclude) == {"1. FOR CONSTRUCTION/1.2 STRUCTURE/photos"}
    batches = survey.plan(root, include, exclude)
    assert [(b["label"], b["files"], b["exclude"]) for b in batches] == [
        ("1. FOR CONSTRUCTION", 4, ["1.2 STRUCTURE/photos"]), ("2. SHOPDRAWING", 1, [])]


def test_folder_too_big_for_one_batch_is_split(share, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_SOURCE_FILES", 2)
    root = survey.scan(share)
    batches = survey.plan(root, *survey.rule_pick(root))
    assert [(b["label"], b["files"], b["exclude"], b["too_big"]) for b in batches] == [
        ("1. FOR CONSTRUCTION › 1.1 ARCHITECTURE", 2, [], False),
        ("1. FOR CONSTRUCTION › 1.2 STRUCTURE", 1, ["photos"], False),
        ("1. FOR CONSTRUCTION（这一层直接放的文件）", 1, ["1.1 ARCHITECTURE", "1.2 STRUCTURE"], False),
        ("2. SHOPDRAWING", 1, [], False)]


def _ai(prompt, n):
    """像模型一样：要“FOR CONSTRUCTION”整个收、下面的照片不要，深化图也收；顺手给个不存在的编号。"""
    rows = {m.group(2): int(m.group(1)) for m in re.finditer(r"^(\d+)\|([^|]+)\|", prompt, re.M)}
    return {"include": [{"i": rows["1. FOR CONSTRUCTION"], "reason": "施工图"},
                        {"i": rows["1. FOR CONSTRUCTION/1.1 ARCHITECTURE"], "reason": "上一层已经收了"},
                        {"i": rows["2. SHOPDRAWING"], "reason": "深化图"}, {"i": 999, "reason": "编的"}],
            "exclude": [{"i": rows["1. FOR CONSTRUCTION/1.2 STRUCTURE/photos"], "reason": "照片"},
                        {"i": rows["0. EMAIL"], "reason": "本来就没收"}]}, None


def test_codex_decides_which_folders(share, proj):
    fake = FakeCodex(_ai)
    lines = []
    p = survey.make_plan(proj, share, runner=fake, log=lines.append)
    assert p["how"].startswith("Codex") and set(p["include"]) == {"1. FOR CONSTRUCTION", "2. SHOPDRAWING"}
    assert p["exclude"] == {"1. FOR CONSTRUCTION/1.2 STRUCTURE/photos": "照片"}
    assert [b["label"] for b in p["batches"]] == ["1. FOR CONSTRUCTION", "2. SHOPDRAWING"]
    assert {x["rel"] for x in p["not_taken"]} == {"0. EMAIL", "8. MEETING - MOM"}
    sent = fake.calls[0]["prompt"]
    assert "建施-01 一层平面图.pdf" in sent and "re 答复" not in sent  # 只发文件夹名、数量和几个图纸文件名
    saved = json.loads(Path(p["file"]).read_text(encoding="utf-8"))
    assert saved["batches"] == p["batches"] and Path(p["file"]).with_suffix(".md").exists()
    assert any("收 2 批、5 个文件" in ln for ln in lines)


def test_codex_failing_falls_back_to_rules(share, proj):
    p = survey.make_plan(proj, share, runner=FakeCodex(lambda p, n: (None, "stream disconnected")), log=lambda s: None)
    assert p["how"] == "按文件夹名字和数量" and [b["label"] for b in p["batches"]] == ["1. FOR CONSTRUCTION", "2. SHOPDRAWING"]


def test_receive_by_plan_and_again(share, proj):
    p = survey.make_plan(proj, share, "rules", log=lambda s: None)
    lines = []
    r = survey.receive_plan(proj, p, "2026-10-22", av=False, log=lines.append)
    assert [x["batch"] for x in r["received"]] == [1, 2] and not r["failed"]
    with proj.connect() as con:
        names = [x[0] for x in con.execute("SELECT name FROM batches ORDER BY seq")]
        paths = [x[0] for x in con.execute("SELECT path FROM occurrences ORDER BY id")]
    assert names == ["1. FOR CONSTRUCTION", "2. SHOPDRAWING"]
    assert "批次#1 / 1. FOR CONSTRUCTION（文件夹） / 1.2 STRUCTURE / 结施-01 结构总说明.pdf" in paths
    assert not any("photos" in x for x in paths)
    again = survey.receive_plan(proj, p["file"], "2026-10-22", av=False, log=lines.append)
    assert len(again["skipped"]) == 2 and not again["received"]  # 断了再按计划收：收过的批次跳过


def test_receive_plan_goes_on_when_the_database_is_busy(share, proj, monkeypatch):
    """收某一批时数据库一直被别的程序占着（工作台开着在算）：这一批记成没收成，接着收下一批，不整个停下。"""
    import sqlite3

    from bidbench import ops, store
    assert store.DB_TIMEOUT >= 60  # 别人正在写，先等一阵
    p = survey.make_plan(proj, share, "rules", log=lambda s: None)
    real, calls = ops.receive, []

    def receive(*a, **kw):
        calls.append(kw["label"])
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(ops, "receive", receive)
    lines = []
    r = survey.receive_plan(proj, p, "2026-10-22", av=False, log=lines.append)
    assert [x["batch"] for x in r["failed"]] == [1] and [x["batch"] for x in r["received"]] == [2]
    assert any("database is locked" in x and "再按计划收一次会接着收" in x for x in lines)


def test_cli_survey_and_receive_plan(share, proj, capsys):
    cli.main(["-p", str(proj.root), "survey", str(share), "--executor", "rules"])
    out = capsys.readouterr().out
    assert "收 2 批" in out and "收图计划：" in out
    plan_file = sorted((proj.root / "reports").glob("收图计划-*.json"))[-1]
    cli.main(["-p", str(proj.root), "receive-plan", str(plan_file), "--date", "2026-10-22", "--no-av", "--only", "2"])
    assert "收了 1 批" in capsys.readouterr().out
    titles = [e["title"] for e in oplog.read(proj)]
    assert titles[0] == f"按计划收图：{plan_file.name}" and titles[1] == "智能收图规划：OASIS"


def test_workbench_survey_then_receive_plan(share, proj, monkeypatch):
    """工作台：智能收图出计划，勾几批按计划收；一批收不下的普通收图提示改用智能收图。"""
    import threading

    from test_desktop import Client

    from bidbench import ops, server
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: {"ready": False, "hint": "没装"})
    httpd = server.serve(proj, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        assert c.post("/api/job", {"kind": "survey", "path": str(share / "说明.txt")})["_status"] == 400
        j = c.job("survey", path=str(share))
        assert j["status"] == "done" and j["title"] == "智能收图规划：OASIS", j
        plan = j["result"]
        assert [b["label"] for b in plan["batches"]] == ["1. FOR CONSTRUCTION", "2. SHOPDRAWING"]
        j = c.job("receive_plan", plan=plan["file"], only=[2], date="2026-10-22", av=False)
        assert j["status"] == "done" and j["result"]["received"] == [{"batch": 2, "seq": 1, "files": 1}], j
        st = c.get("/api/state")
        assert [b["name"] for b in st["batches"]] == ["2. SHOPDRAWING"]
        monkeypatch.setattr(ingest, "MAX_SOURCE_FILES", 3)
        j = c.job("receive", paths=[str(share)], date="2026-10-22", av=False)
        assert j["status"] == "error" and "一批收不下" in j["error"]
    finally:
        httpd.shutdown()


def test_folder_dates_and_order():
    """历次接收图纸的文件夹名字里有日期：按日期排、用它当收图日期；招标图最前，现行整套放最后。"""
    assert survey.date_in("008 RECORD/Forcont Archi/建筑001-20240308-接收图纸-新增") == "2024-03-08"
    assert survey.date_in("MEP/7.#75_2024.08.01/补充") == "2024-08-01"
    assert survey.date_in("2024-03 季度/会议 20251301") is None  # 不是日期的数字串不算
    assert survey.date_in("000-FOR-CON DRAWING COMBINED") is None

    def b(label, date=None):
        return {"label": label, "date": date}
    got = survey.in_order([b("000-FOR-CON DRAWING COMBINED"), b("STR › 结构002", "2024-06-11"), b("007-TENDER DRAWING"),
                           b("ARCHI › 建筑001", "2024-03-08"), b("009-SHOP DRAWING"), b("MEP › 机电001", "2024-05-01")])
    assert [x["label"] for x in got] == ["007-TENDER DRAWING", "ARCHI › 建筑001", "MEP › 机电001", "STR › 结构002",
                                         "000-FOR-CON DRAWING COMBINED", "009-SHOP DRAWING"]


def test_dated_folders_are_received_in_date_order_with_their_dates(tmp_path, proj):
    top = tmp_path / "记录"
    for rel in ("现行汇总/建施-01.pdf", "结构/结构002-20240611-接收图纸-变更/结施-01.pdf",
                "建筑/建筑001-20240308-接收图纸-新增/建施-02.pdf"):
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_bytes(_pdf(Path(rel).stem, "图"))
    root = survey.scan(top)
    include = {"现行汇总": "整套", "结构/结构002-20240611-接收图纸-变更": "接收", "建筑/建筑001-20240308-接收图纸-新增": "接收"}
    p = {"batches": survey.plan(root, include, {})}
    survey.receive_plan(proj, p, "2026-10-22", av=False, log=lambda s: None)
    with proj.connect() as con:
        rows = [tuple(r) for r in con.execute("SELECT name, received_at FROM batches ORDER BY seq")]
    assert rows == [("建筑 › 建筑001-20240308-接收图纸-新增", "2024-03-08"), ("结构 › 结构002-20240611-接收图纸-变更", "2024-06-11"),
                    ("现行汇总", "2026-10-22")]


def test_folder_of_dated_deliveries_is_split_per_delivery(tmp_path):
    """放得下也拆：下面是一次次接收的图纸（名字里有日期），每次一批，各用各的日期。"""
    top = tmp_path / "共享"
    for rel in ("结构/结构001-20240308-接收图纸-新增/结施-01.pdf", "结构/结构002-20240613-接收图纸-变更/结施-01.pdf",
                "结构/说明.pdf", "建筑/平面/建施-01.pdf"):
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_bytes(_pdf(Path(rel).stem, "图"))
    root = survey.scan(top)
    got = survey.plan(root, {"结构": "施工图", "建筑": "施工图"}, {})
    assert [(b["label"], b["date"]) for b in got] == [
        ("结构 › 结构001-20240308-接收图纸-新增", "2024-03-08"), ("结构 › 结构002-20240613-接收图纸-变更", "2024-06-13"),
        ("建筑", None), ("结构（这一层直接放的文件）", None)]
