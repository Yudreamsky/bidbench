"""建模找图评测（samples/find_eval.py）：打分本身对不对，现有工具的分数不能往下掉。"""

import find_eval
import make_model_sample
import pytest

# 现在 card 方法的得分。工具或识别改好了、分数涨了，把这里改成新的分数；分数掉了说明找图变差了。
FLOOR = {"need": 11, "related": 8, "missing": 1, "notices": 0}


@pytest.fixture(scope="module")
def drill(tmp_path_factory):
    return find_eval.build_project(tmp_path_factory.mktemp("drill"))


def test_every_start_sheet_exists(drill):
    tk = find_eval.Toolkit(drill)
    for task in make_model_sample.TASKS:
        assert tk.get_sheet(task["start_sheet"])["status"] == "现行", task["id"]


def test_oracle_scores_full_marks(drill):
    """照着答案交卷的找图方法应该拿满分、不多给。"""
    def oracle(tk, task):
        return {"sheets": task["need"] + task["related"], "missing": task["missing"], "notices": task["notices"]}
    find_eval.FINDERS["oracle"] = oracle
    try:
        res = find_eval.evaluate(drill, "oracle")
    finally:
        del find_eval.FINDERS["oracle"]
    assert res["score"] == 100 and res["hit"] == res["want"] == 25
    assert res["extra"] == 0 and res["tool_calls"] == 0


def test_score_task_counts_hits_misses_and_extras():
    task = {"id": "X", "task": "试", "start_sheet": "建施-02", "need": ["建施-02", "建施-06"], "related": ["建施-07"],
            "missing": ["建施-09"], "notices": ["设计变更单-003"]}
    r = find_eval.score_task(task, {"sheets": ["建施-02", "建施-07", "结施-01"], "missing": [], "notices": []})
    assert r["kinds"]["need"]["miss"] == ["建施-06"]
    assert r["kinds"]["related"]["hit"] == ["建施-07"]
    assert r["kinds"]["missing"]["miss"] == ["建施-09"]
    assert r["kinds"]["notices"]["miss"] == ["设计变更单-003"]
    assert r["extra"] == ["结施-01"]


def test_card_finder_does_not_regress(drill):
    res = find_eval.evaluate(drill, "card")
    got = {k: v["hit"] for k, v in res["totals"].items()}
    for kind, floor in FLOOR.items():
        assert got[kind] >= floor, f"{kind} 从 {floor} 掉到了 {got[kind]}：\n{find_eval.report(res)}"
    assert res["tool_calls"] == 2 * len(make_model_sample.TASKS)  # 每个任务一次 get_sheet、一次 list_sheets
    text = find_eval.report(res)
    assert text.startswith("建模找图评测（找图方法：card）") and "T5 核对楼层标高" in text


def test_unknown_start_sheet_is_reported_not_raised(drill):
    task = dict(make_model_sample.TASKS[0], start_sheet="建施-99")
    res = find_eval.evaluate(drill, "card", [task])
    assert "没有图号 建施-99" in res["tasks"][0]["error"] and res["hit"] == 0


def test_cli_writes_json(drill, tmp_path, capsys):
    out = tmp_path / "结果.json"
    res = find_eval.main(["--project", str(drill.root), "--json", str(out)])
    assert out.exists() and '"score"' in out.read_text(encoding="utf-8")
    assert f"总分 {res['score']} 分" in capsys.readouterr().out
