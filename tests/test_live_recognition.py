"""识别按优先级排队、估算不渲染图片、识别途中分批导入、还没识别到的图不算缺图。用假的 Codex，不联网、不占额度。"""

import json
import threading

import make_sample
import pytest
from test_codex_executor import FakeCodex, _body_from_prompt

from bidbench import ops, recognition, server, state


@pytest.mark.parametrize("path,key", [
    ("批次#1 / x.zip / 建筑 / 建施-00 建筑图纸目录.pdf", "catalog"),
    ("批次#1 / 建施-01 建筑设计总说明.pdf", "notes"),
    ("批次#1 / 结构 / 结施-03 二层梁配筋图.pdf", "plans"),
    ("批次#1 / 建施-05 1-1剖面图.pdf", "sections"),
    ("批次#1 / 建筑 / 详图 / 建施-07~08、10 详图合订本.pdf", "details"),
    ("批次#1 / 楼梯平面详图.pdf", "details"),  # 详图先于平面认
    ("批次#1 / 建施-06 门窗表.pdf", "details"),
    ("批次#1 / Drawings / PRJ1-ARC-ZZ-00-DR-A-1001_Ground Floor GA Plan.pdf", "plans"),
    ("批次#1 / Drawings / North Elevation.pdf", "sections"),
    ("批次#1 / Drawings / Drawing Register.pdf", "catalog"),
    ("批次#1 / Drawings / General Notes.pdf", "notes"),
    ("批次#1 / Details / PRJ1-ARC-XX-DR-A-5001.pdf", "details"),  # 文件名认不出，看所在文件夹
    ("批次#1 / Drawings / Plant Room Layout.pdf", "plans"),
    ("批次#1 / Drawings / PRJ1-STR-ZZ-DR-S-0001.pdf", "other"),
])
def test_priority_of(path, key):
    assert recognition.priority_of(path) == key


def _tasks(proj):
    d = proj.root / "recognition" / "tasks"
    return sorted(d.iterdir()) if d.exists() else []


def test_plan_and_task_follow_priority(demo_copy):
    before = _tasks(demo_copy)
    plan = ops.plan_recognition(demo_copy, "all", "codex")
    assert _tasks(demo_copy) == before  # 估算不生成任务包、不渲染图片
    keys = [g["key"] for g in plan["priorities"]]
    assert keys == sorted(keys, key=recognition.PRIORITY_RANK.get) and len(keys) >= 3
    assert plan["pages"] == sum(g["pages"] for g in plan["priorities"]) and all(g["minutes"] >= 1 for g in plan["priorities"])
    assert "第二步 重点图" in ops.estimate_text(plan)
    seen = []
    task = recognition.make_task(demo_copy, scope="all", render=False, priorities=keys[:2], progress=lambda n, t: seen.append((n, t)))
    items = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    assert {it["priority"] for it in items} == set(keys[:2])  # 只放选中的几档
    assert len(items) == sum(g["pages"] for g in plan["priorities"][:2]) and seen[-1] == (len(items), len(items))
    ranks = [recognition.PRIORITY_RANK[it["priority"]] for it in items]
    assert ranks == sorted(ranks)  # 先识别排在前面的档
    with pytest.raises(ValueError, match="优先级"):
        recognition.make_task(demo_copy, scope="all", render=False, priorities=["xx"])
    claude_plan = ops.plan_recognition(demo_copy, "all", "claude")
    assert claude_plan["estimated_usd"] > 0 and all(g["usd"] > 0 for g in claude_plan["priorities"])


def test_results_imported_while_recognizing(demo_copy, monkeypatch):
    monkeypatch.setattr(ops, "LIVE_IMPORT_PAGES", 2)
    task = recognition.make_task(demo_copy, scope="all", render=False)
    done_in_db, imported = [], []

    def progress(n, total):
        with demo_copy.connect() as con:
            done_in_db.append(con.execute("SELECT COUNT(*) FROM recognitions WHERE executor='codex'").fetchone()[0])
    r = ops.recognize_codex(demo_copy, task, runner=FakeCodex(), workers=1, log=lambda s: None, progress=progress,
                            on_import=imported.append)
    assert r["done"] > 4 and r["imported"] == r["done"]
    assert any(0 < n < r["done"] for n in done_in_db)  # 还没识别完，前面识别好的已经在项目里了
    assert imported == sorted(imported) and imported[-1] == r["done"]


def test_stopped_run_keeps_recognized_pages(demo_copy, monkeypatch):
    monkeypatch.setattr(ops, "LIVE_IMPORT_PAGES", 1000)  # 途中不导入：停下时也要把已识别的导进来
    task = recognition.make_task(demo_copy, scope="all", render=False)
    limited = FakeCodex(lambda p, n: (_body_from_prompt(p), None) if n <= 3 else (None, "You've hit your usage limit."))
    with pytest.raises(ops.OpError, match="已识别的 3 页已导入"):
        ops.recognize_codex(demo_copy, task, runner=limited, workers=1, log=lambda s: None)
    with demo_copy.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM recognitions WHERE executor='codex'").fetchone()[0] == 3


def test_unrecognized_sheets_are_not_missing(sheets_project):
    """平面图引用了大样，大样那页还没识别（只识别了前几档）：先不算缺图，等识别完再说。"""
    def pdf(no, title, *body):
        return make_sample.sheet_pdf(no, title, "A", "2026-01-01", list(body), [("A", "2026-01-01", "出图")])
    proj = sheets_project({
        "建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图", "墙身详见建施-07", "卫生间详见建施-09"),
        "建施-07 墙身大样.pdf": pdf("建施-07", "墙身大样"),
    })
    with proj.connect() as con:  # 大样那页还没识别
        con.execute("DELETE FROM recognitions WHERE page_id IN (SELECT p.page_id FROM pages p JOIN occurrences o"
                    " ON o.sha256 = p.file_sha256 WHERE o.path LIKE '%建施-07%')")
    st = state.build(proj)
    missing = {i["title"].split()[0] for i in st["issues"] if i["kind"] == "缺图"}
    assert missing == {"建施-09"}  # 真的没有的照样报缺图
    pend = next(i for i in st["issues"] if i["id"] == "pending_refs")
    assert pend["level"] == "info" and "建施-07 墙身大样.pdf" in pend["detail"]
    by = {s["no"]: s for s in st["sheets"]}
    assert by["建施-07"]["status"] == "guess" and by["建施-07"]["fields"]["no"]["conf"] == "high"  # 按文件名暂定，PDF 文字里也有这个图号
    assert by["建施-09"]["status"] == "missing"


def test_state_version_tells_which_job(demo_copy):
    from test_desktop import Client
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert Client(httpd).get("/api/state/version")["job"] is None  # 没有任务；识别时是 recognize，界面跟着刷新
    finally:
        httpd.shutdown()
