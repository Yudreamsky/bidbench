"""分步识别（docs/图纸字段.md 9.8）：地图 → 重点图 → 建模主图 → 按需深入，每页归哪一步、进度、按步识别。"""

import json

import make_sample
import pytest
from conftest import _recognize

from bidbench import assistant, cli, ingest, ops, recognition
from bidbench.executors import simulated
from bidbench.store import Project


@pytest.fixture
def staged(tmp_path):
    root, date = make_sample.build_staged(tmp_path / "in")
    proj = Project.init(tmp_path / "p", "分步识别")
    ingest.receive(proj, root, date)
    ops.read_cad(proj, log=lambda *a: None)
    return proj


def _groups(proj) -> dict[str, str]:
    """出处（文件名，多页的带页码）→ 归哪一组。"""
    with proj.connect() as con:
        rows = recognition.pages_to_recognize(con, scope="all")
        hints = recognition.map_hints(con)
    return {recognition.page_path(r).split(" / ")[-1]: recognition.stage_group(r, hints) for r in rows}


def test_pages_sorted_into_stages(staged):
    g = _groups(staged)
    assert g["A-001 图纸目录 DRAWING LIST.pdf"] == "catalog"
    assert g["A-601 DOOR SCHEDULE.pdf"] == "lists"
    assert g["Tender Query Response 01.pdf"] == "changes"
    assert [g[f"Specification - Lighting.pdf（第 {i} 页）"] for i in range(1, 6)] == ["notes_head"] * 2 + ["notes_body"] * 3
    assert g["A-101 LEVEL 01 PLAN.pdf"] == "plans" and g["A-301 SECTION A-A.pdf"] == "sections"
    assert g["A-501 STAIR DETAILS.pdf"] == "details"
    assert g["A-103 LEVEL 03 PLAN.pdf"] == "dwg"  # 同图号的 DXF 已读出图签和文字
    assert g["RIBA 4 BINDER.pdf（第 1 页）"] == "catalog"  # 合订本开头写着目录
    assert g["RIBA 4 BINDER.pdf（第 2 页）"] == "other"  # 只认出图号，还不知道是什么图


def test_plan_progress_and_map_guides_later_stages(staged):
    sp = recognition.stage_plan(staged)
    rows = sp.pop("_rows")
    sp.pop("_hints")
    st = {x["key"]: x for x in sp["stages"]}
    assert [x["key"] for x in sp["stages"]] == ["key", "main", "deep"]
    assert (st["key"]["total"], st["main"]["total"], st["deep"]["total"]) == (6, 2, 6)
    assert all(x["done"] == 0 and x["left"] == x["total"] for x in st.values()) and len(rows) == 14
    assert [g["key"] for g in st["key"]["groups"]] == ["catalog", "changes", "lists", "notes_head"]
    m = sp["map"]
    assert m["pages"] == 14 and m["numbered"] >= 8 and m["dwg_files"] == 1 and m["dwg_sheets"] == 1

    # 只识别第二步：任务包里只有重点图，目录在最前
    task = recognition.make_task(staged, render=False, stages=["key"])
    items = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    assert len(items) == 6 and items[0]["source"]["file_path"].endswith("A-001 图纸目录 DRAWING LIST.pdf")
    simulated.run(task)
    assert not recognition.import_results(staged, task / "results").errors

    # 重点图识别完：进度记上（模拟执行器的结果在 new 范围里算没识别，这里按 unrecognized 看）；
    # 目录上写着 A-102 是 LEVEL 02 PLAN，合订本第 2 页跟着归到建模主图
    sp = recognition.stage_plan(staged, "unrecognized")
    st = {x["key"]: x for x in sp["stages"]}
    assert (st["key"]["done"], st["key"]["left"]) == (6, 0)
    assert st["main"]["total"] == 3 and st["main"]["left"] == 3 and st["deep"]["total"] == 5
    assert _groups(staged)["RIBA 4 BINDER.pdf（第 2 页）"] == "plans"
    assert recognition.make_task(staged, render=False, scope="unrecognized", stages=["key"]) is None  # 接着来：做完的不再识别
    with pytest.raises(ValueError, match="未知的识别步骤"):
        recognition.pick_pages(staged, stages=["xx"])


def test_estimate_text_cli_and_assistant(staged, capsys):
    est = ops.plan_recognition(staged, "new", "codex")
    assert [x["key"] for x in est["stages"]] == ["key", "main", "deep"] and all(x["minutes"] >= 1 for x in est["stages"])
    text = ops.estimate_text(est)
    assert "第一步 地图" in text and "第二步 重点图：还要识别 6 页" in text and "第四步 按需深入" in text
    claude_est = ops.plan_recognition(staged, "new", "claude")
    assert all(x["usd"] > 0 for x in claude_est["stages"])

    cli.main(["-p", str(staged.root), "stages"])
    assert "第三步 建模主图：还要识别 2 页" in capsys.readouterr().out

    out = assistant.Toolkit(staged).estimate_recognition()
    assert [s["name"] for s in out["stages"]] == ["重点图", "建模主图", "按需深入"] and out["map"]["step"] == 1
    assert "--stage key" in out["command"]


def test_demo_estimate_has_stages(demo_copy):
    _recognize(demo_copy)
    est = ops.plan_recognition(demo_copy, "all", "codex")
    assert sum(x["left"] for x in est["stages"]) == est["pages"]
