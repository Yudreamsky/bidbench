"""给 AI 建模找图的导航工具：按任务找图、顺着关系往外走、跳到页面上的位置、数据镜像里的关系。

用 make_model_sample.py 的演练办公楼（模拟执行器识别）：建二层结构梁、一层墙体门窗、楼梯、屋面这几个任务。"""

import asyncio
import json

import pytest

import make_model_sample
from bidbench import export, floors, ingest, navigate, recognition, state
from bidbench.assistant import Toolkit, ToolError
from bidbench.executors import simulated
from bidbench.store import Project


@pytest.fixture(scope="module")
def drill(tmp_path_factory):
    root = tmp_path_factory.mktemp("drill")
    proj = Project.init(root / "p", make_model_sample.PROJECT)
    for path, date in make_model_sample.build(root / "zips"):
        ingest.receive(proj, path, date)
        task = recognition.make_task(proj, render=False)
        simulated.run(task)
        assert not recognition.import_results(proj, task / "results").errors
    return proj


@pytest.fixture
def kit(drill):
    return Toolkit(drill)


def roles(res):
    return {r["no"]: r["role"] for r in res["sheets"]}


def test_find_beams_on_a_floor(kit):
    res = kit.find_sheets_for_task("二层", "结构", "梁")
    got = roles(res)
    assert got["结施-03"] == "主图" and got["结施-07"] == "按图名找到"  # 梁柱节点详图：没写楼层，图名有“梁”
    assert got["结施-01"] == got["结施-02"] == "通用图" and got["结施-06"] == "引用到的图"
    assert "结施-04" not in got and "结施-05" not in got  # 别的楼层的梁不给
    assert not any(r["discipline"] == "建筑" for r in res["sheets"])
    main = next(r for r in res["sheets"] if r["no"] == "结施-03")
    assert main["current_rev"] == "B" and main["page_id"] and main["files"]["pdf"]  # 升过版：给现行的 B 版
    assert "结施-03 上写“详见结施-06”" in next(r for r in res["sheets"] if r["no"] == "结施-06")["why"]
    assert res["query"]["floor"] == "2F"


def test_find_walls_reports_missing_sheet(kit):
    res = kit.find_sheets_for_task("1F", "建筑", "墙 门窗")
    got = roles(res)
    assert got["建施-02"] == "主图"  # 这一层的平面图，图名没有关键词也是主图
    assert got["建施-06"] == "按图名找到" and got["建施-01"] == "通用图"
    assert got["建施-05"] == got["建施-09"] == "引用到的图"
    assert res["missing"] == ["建施-09"] and any(i["kind"] == "缺图" for i in res["issues"])


def test_find_details_without_floor_lists_where_used(kit):
    got = roles(kit.find_sheets_for_task(keyword="楼梯"))
    assert got["建施-08"] == got["结施-06"] == "主图"
    assert got["建施-02"] == "引用它的图"  # 一层平面上写了“楼梯详见建施-08”
    assert "结施-02" in got and got["结施-02"] == "通用图"  # 主图里有结构的图，带上结构的通用图


def test_find_roof_without_discipline(kit):
    got = roles(kit.find_sheets_for_task("屋面"))
    assert {no for no, r in got.items() if r == "主图"} == {"建施-04", "建施-10", "结施-05"}
    assert got["建施-01"] == "通用图"


def test_find_errors_and_floor_words(kit, drill):
    with pytest.raises(ToolError, match="至少给出"):
        kit.find_sheets_for_task()
    with pytest.raises(ToolError, match="认不出楼层"):
        kit.find_sheets_for_task("某某", "结构")
    st = state.build(drill)
    for word in ("2", "2层", "二层", "2F"):
        assert navigate.parse_floor(st, word) == {2}
    assert navigate.parse_floor(st, "屋面") == {floors.ROOF} and navigate.parse_floor(st, "-1") == {-1}


def test_walk_relations(kit):
    out = kit.related_sheets("建施-02", types=["引用"], direction="out")
    assert {r["no"] for r in out["sheets"]} == {"建施-05", "建施-06", "建施-08", "建施-09"}
    assert out["missing"] == ["建施-09"] and all(r["distance"] == 1 for r in out["sheets"])
    via = next(r for r in out["sheets"] if r["no"] == "建施-08")["via"][0]
    assert via["edge"] == "建施-02 → 建施-08" and via["text"] == "详见建施-08" and len(via["bbox"]) == 4

    two = kit.related_sheets("建施-02", depth=2, types=["引用", "跨专业引用"], direction="out")
    far = next(r for r in two["sheets"] if r["no"] == "结施-06")
    assert far["distance"] == 2 and far["via"][0]["edge"] == "建施-08 → 结施-06"
    assert far["via"][0]["relation"] == "跨专业引用"

    back = kit.related_sheets("建施-08", direction="in", types=["引用", "跨专业引用"])
    assert {r["no"] for r in back["sheets"]} == {"建施-01", "建施-02", "建施-03", "结施-06"}

    around = kit.related_sheets("结施-03")  # 默认全部种类：同层、通用图只从起点算一步
    rel = {r["no"]: {v["relation"] for v in r["via"]} for r in around["sheets"]}
    assert rel["建施-03"] == {"同层"} and rel["结施-01"] == {"通用图"} and rel["结施-07"] == {"引用"}
    assert all(r["distance"] == 1 for r in around["sheets"])

    with pytest.raises(ToolError, match="关系种类可选"):
        kit.related_sheets("建施-02", types=["剖切"])
    with pytest.raises(ToolError, match="没有图号"):
        kit.related_sheets("建施-99")


def test_walk_from_change_notice(demo_copy):
    kit = Toolkit(demo_copy)
    n = next(x for x in kit._state()["notices"] if x["affects"])
    out = kit.related_sheets(n["id"])
    assert out["start"]["kind"] == n["kind"] and {r["no"] for r in out["sheets"]} >= set(n["affects"])
    assert out["sheets"][0]["via"][0]["relation"] == "变更单"
    back = kit.related_sheets(n["affects"][0], types=["变更单"])
    assert [r["no"] for r in back["sheets"]] == [n["id"]] and back["sheets"][0]["page_id"] == n["page_id"]


def test_locate_region_and_old_version(kit):
    card = kit.get_sheet("建施-02")
    ref = next(r for r in kit.related_sheets("建施-02", types=["引用"])["sheets"] if r["no"] == "建施-08")["via"][0]
    out = kit.locate(card["current_page_id"], ref["bbox"])
    assert out["sheet"] == "建施-02" and out["version_state"] == "现行" and "warning" not in out
    assert out["page_number"] == 1 and out["file"].endswith(".pdf") and out["page_size_pt"][0] > 0
    assert "建施-08" in out["region"]["text"]
    assert [r["to"] for r in out["region"]["references_here"]] == ["建施-08"]
    assert len(out["references_on_page"]) >= 4

    old = next(v for v in kit.get_sheet("结施-03")["versions"] if v["state"] != "现行")
    out = kit.locate(old["page_id"])
    assert "现行是 B 版" in out["warning"] and "region" not in out

    with pytest.raises(ToolError, match="bbox"):
        kit.locate(card["current_page_id"], [0.5, 0.5, 0.2, 0.9])
    with pytest.raises(ToolError, match="格式"):
        kit.locate("../x")
    with pytest.raises(ToolError, match="没有页面"):
        kit.locate("000000000000-p001")


def test_mirror_relations(drill, tmp_path):
    st = state.build(drill)
    target = export.export_mirror(drill, st, tmp_path / "mirror")
    doc = json.loads((target / "relations.json").read_text(encoding="utf-8"))
    assert doc["sheets"]["结施-03"]["current_rev"] == "B" and doc["sheets"]["建施-09"]["status"] == "缺失"
    assert {"from": "建施-02", "to": "建施-08", "type": "引用"}.items() <= next(
        r for r in doc["relations"] if r["from"] == "建施-02" and r["to"] == "建施-08").items()
    assert "建施-03" in doc["same_floor"]["结施-03"] and "结施-01" in doc["general"]["结施-03"]
    assert set(doc["relation_types"]) == set(navigate.REL_LABEL.values())
    card = (target / "L1-图纸卡片" / "结施-03.md").read_text(encoding="utf-8")
    assert "## 同层图纸（2F）" in card and "建施-03" in card and "## 通用图" in card
    assert "relations.json" in (target / "README.md").read_text(encoding="utf-8")


def test_mcp_navigation_tools(kit):
    pytest.importorskip("mcp")
    from bidbench.mcp_server import build_server

    server = build_server(kit)

    async def go():
        tools = {t.name for t in await server.list_tools()}
        assert {"find_sheets_for_task", "related_sheets", "locate"} <= tools
        r = await server.call_tool("find_sheets_for_task", {"floor": "2F", "discipline": "结构", "keyword": "梁"})
        content = r.content if hasattr(r, "content") else r[0] if isinstance(r, tuple) else r
        assert json.loads(content[0].text)["sheets"][0]["no"] == "结施-03"
        r = await server.call_tool("related_sheets", {"no": "建施-02", "types": ["剖切"]})
        content = r.content if hasattr(r, "content") else r[0] if isinstance(r, tuple) else r
        assert "关系种类可选" in content[0].text
    asyncio.run(go())
