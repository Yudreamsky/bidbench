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
    assert {r["no"] for r in out["sheets"]} == {"建施-05", "建施-06", "建施-07", "建施-08", "建施-09"}
    idx = next(r for r in out["sheets"] if r["no"] == "建施-07")["via"]  # 画的圆圈详图索引 ①、③
    assert {(v["form"], v["mark"]) for v in idx} == {("详图索引", "1"), ("详图索引", "3")}
    assert all(v["target"]["page_id"] and len(v["target"]["bbox"]) == 4 for v in idx)
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


# ---- 关联分种类、详图编号、分区和轴线范围（docs/图纸字段.md 9.6、第 7 步） ----

def test_typed_relations_and_jump_to_detail(kit, drill):
    """圆圈详图索引带编号，跳到合订本里那块详图；剖切带剖面编号；“详见”是文字引用。"""
    st = state.build(drill)
    forms = {(e["from"], e["to"], e["form"], e["mark"]) for e in st["edges"]}
    assert ("建施-02", "建施-07", "详图索引", "1") in forms and ("建施-04", "建施-07", "详图索引", "2") in forms
    assert ("建施-02", "建施-05", "剖切", "1-1") in forms and ("建施-02", "建施-05", "剖切", "2-2") in forms
    assert ("建施-02", "建施-08", "文字引用", None) in forms
    assert not any(e["to"] == "建施-05" and e["form"] == "文字引用" and e["from"] == "建施-02" for e in st["edges"])

    card = kit.get_sheet("建施-04")
    ref = next(r for r in card["references"] if r["sheet"] == "建施-07")
    assert ref["form"] == "详图索引" and ref["mark"] == "2" and ref["jump_to"]["title"] == "女儿墙大样"
    page = kit.locate(ref["jump_to"]["page_id"], ref["jump_to"]["bbox"])
    assert "女儿墙大样" in json.dumps(page, ensure_ascii=False)
    detail = kit.get_sheet("建施-07")["details"]
    assert [(d["mark"], d["title"], d["scale"]) for d in detail] == [
        ("1", "外墙墙身大样", "1:20"), ("2", "女儿墙大样", "1:20"), ("3", "窗台节点", "1:10")]

    res = kit.find_sheets_for_task("屋面", "建筑")
    jump = next(r for r in res["sheets"] if r["no"] == "建施-07")["jump_to"]
    assert [j["mark"] for j in jump] == ["2"]


def test_change_notice_lists_changed_sheets_and_details(kit, drill):
    """变更单条文里写到的图号就是它改的图，写到 ② 就是改的哪块详图。"""
    st = state.build(drill)
    n = next(n for n in st["notices"] if n["id"] == "设计变更单-003")
    assert n["affects"] == ["建施-07", "结施-03"]
    assert {(r["to"], r["mark"]) for r in n["refs"]} == {("结施-03", None), ("建施-07", "2")}
    assert kit.get_sheet("结施-03")["status"] == "有变更单"
    out = kit.related_sheets("设计变更单-003")
    via = next(r for r in out["sheets"] if r["no"] == "建施-07")["via"][0]
    assert via["form"] == "变更涉及" and via["mark"] == "2" and "女儿墙" in via["text"]
    assert via["target"]["title"] == "女儿墙大样"


def test_coverage_axes(kit):
    """平面图读出轴线范围；详图上的 ①②③、目录的版次栏不是轴网。"""
    assert kit.get_sheet("建施-02")["covers"] == "1~5/A~D 轴"
    assert kit.get_sheet("建施-02")["fields"]["axis_x"]["value"] == "1~5"
    for no in ("建施-07", "建施-00", "结施-00"):
        assert kit.get_sheet(no)["covers"] is None, no
    row = next(r for r in kit.find_sheets_for_task("1F", "建筑")["sheets"] if r["no"] == "建施-02")
    assert row["covers"] == "1~5/A~D 轴"


def test_zone_and_stacked_index_from_text_layer():
    """分区从图名读；圆圈详图索引上下两行（编号、图号）拼成一条；只有一个方向的数字不当轴网。"""
    def ln(text, x0, y0, x1, y1):
        return {"text": text, "bbox": [x0, y0, x1, y1]}
    item = {"page_id": "0" * 12 + "-p001", "source": {"file_path": "a.pdf", "file_sha256": "0" * 64, "page_index": 1,
                                                      "page_count": 1, "has_text_layer": True}}
    lines = [ln("图名 A区一层平面图", 0.67, 0.76, 0.73, 0.77), ln("图号 建施-11", 0.67, 0.78, 0.73, 0.79)]
    lines += [ln(str(i), 0.1 * i, 0.07, 0.1 * i + 0.01, 0.08) for i in range(1, 7)]
    lines += [ln(c, 0.04, 0.1 * k + 0.1, 0.05, 0.1 * k + 0.11) for k, c in enumerate("ABCD")]
    lines += [ln("⑤", 0.3, 0.5, 0.31, 0.51), ln("建施-12", 0.29, 0.52, 0.32, 0.53)]
    rec = simulated.recognize_page(item, lines)
    cov = {k: v["value"] for k, v in rec["content"]["coverage"].items()}
    assert cov == {"zone": "A区", "axis_x": "1~6", "axis_y": "A~D"}
    assert [(r["ref_type"], r["target_drawing_no"], r["mark"]) for r in rec["references"]] == [("详图索引", "建施-12", "5")]
    assert rec["content"]["xrefs"] == []
    recognition._validator().validate(rec)


def test_shared_xref_relation(drill, monkeypatch):
    """DWG 里共用同一个外部参照的图互相关联（共用参照），只从起点算一步。"""
    real = state.build

    def with_xrefs(*a, **kw):
        st = real(*a, **kw)
        for s in st["sheets"]:
            if s["no"] in ("建施-02", "建施-03"):
                s["xrefs"] = ["CORE - 1F~4F"]
        return st
    monkeypatch.setattr(state, "build", with_xrefs)
    out = Toolkit(drill).related_sheets("建施-02", types=["共用参照"])
    assert [(r["no"], r["via"][0]["text"]) for r in out["sheets"]] == [("建施-03", "CORE - 1F~4F")]
    assert navigate.shared_xrefs(with_xrefs(drill)) == {"CORE - 1F~4F": ["建施-02", "建施-03"]}
