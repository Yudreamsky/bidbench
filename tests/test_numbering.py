"""图号格式：每个项目认一次，确认后才用来定楼栋、去掉版次后缀、认专业和楼层（docs/图纸字段.md 9.5）。"""

import json
import threading
import urllib.error
import urllib.request

import make_sample
import pytest
from conftest import _recognize

from bidbench import ingest, numbering, state
from bidbench.assistant import Toolkit, ToolError
from bidbench.server import serve
from bidbench.store import Project, add_decision

SEGS = ["project", "originator", "building", "discipline", "type", "level", "number", "rev?"]


def _receive(tmp_path, recognize=False):
    root, date = make_sample.build_numbering(tmp_path / "收图")
    proj = Project.init(tmp_path / "p", "英文编号")
    ingest.receive(proj, root, date)
    if recognize:
        _recognize(proj)
    return proj


def _by(st):
    return {s["no"]: s for s in st["sheets"]}


def _confirm(proj, st, **more):
    """按软件猜的格式确认，补上它不敢猜的代码（SE 是安防、LR 是下屋面……）。"""
    fmt = st["numbering"]["format"]
    for role, table in more.items():
        fmt["codes"].setdefault(role, {}).update(table)
    add_decision(proj, "numbering", "*", numbering.decision_value(json.dumps(fmt)))


def test_guess_from_file_names():
    combos = [(b, d, lv) for b in ("T1", "T2") for d in ("AR", "ST", "FP", "SE") for lv in ("GF", "01", "LR")]
    files = [(f"批次#1 / x / PRJ1-ORG-{b}-{d}-DWG-{lv}-{1000 + i:04d}{'-00' if i % 3 == 0 else ''} - PLAN.pdf", None)
             for i, (b, d, lv) in enumerate(combos)]
    items = numbering.items_from(files)
    g = numbering.guess(items)
    assert g["formats"][0]["segments"] == SEGS
    assert g["codes"]["discipline"] == {"AR": "建筑", "ST": "结构", "FP": "消防"}  # SE 可能是结构也可能是安防：不猜
    assert g["codes"]["level"]["GF"] == "Ground Floor" and "LR" not in g["codes"]["level"]
    nb = numbering.Numbering(numbering.validate(g), "guess")
    m = nb.match("PRJ1-ORG-T1-AR-DWG-GF-1000-00")
    assert m["base"] == "PRJ1-ORG-T1-AR-DWG-GF-1000" and m["rev"] == "00" and m["parts"]["building"] == "T1"
    assert nb.coverage(items)["unknown_codes"] == {"discipline": ["SE"], "level": ["LR"]}
    # ISO 英国附录的顺序按固定顺序就读得出，不用问；真要猜也猜得对
    uk = numbering.items_from([(f"批次#1 / PRJ1-ARC-{v}-{lv}-DR-{r}-{1000 + i:04d} - plan.pdf", None) for i, (v, lv, r) in
                               enumerate((v, lv, r) for v in ("T1", "T2") for lv in ("00", "01", "B1") for r in ("A", "S", "M"))])
    assert numbering.need_count(uk) == 0
    assert numbering.guess(uk)["formats"][0]["segments"] == ["project", "originator", "building", "level", "type",
                                                             "discipline", "number"]
    # 专业-塔楼-系列-流水号：只有两位数字的系列号（03 平面、05 详图）不是楼层，不能按楼层换算
    short = numbering.items_from([(f"批次#1 / ST-{b}-{s}-{n:03d}_R1.pdf", None)
                                  for b in ("T1", "T2") for s in ("03", "04", "05", "06") for n in range(1, 13)])
    assert "level" not in numbering.guess(short)["formats"][0]["segments"]


@pytest.mark.parametrize("stem,no,title", [
    ("PRJ1-ORG-T1-ST-DWG-GF-1202 - GROUND FLOOR PLAN", "PRJ1-ORG-T1-ST-DWG-GF-1202", "GROUND FLOOR PLAN"),
    ("PRJ1-ORG-T1-ST-DWG-GF-1202-00 Ground Floor Plan", "PRJ1-ORG-T1-ST-DWG-GF-1202-00", "Ground Floor Plan"),
    ("PRJ1-ORG-T1-ST-DWG-GF-1202-GROUND FLOOR PLAN", "PRJ1-ORG-T1-ST-DWG-GF-1202", "GROUND FLOOR PLAN"),
    ("PRJ1-ORG-T1-ST-DWG-GF-1202-00-EXTERNAL WORKS", "PRJ1-ORG-T1-ST-DWG-GF-1202-00", "EXTERNAL WORKS"),
    ("SUB-ZZ-GEN-XX-1009-01 General Piling Layout", "SUB-ZZ-GEN-XX-1009-01", "General Piling Layout"),
    ("建施-01 一层平面图", None, None),
])
def test_candidate_numbers(stem, no, title):
    got = numbering.candidate(stem)
    assert (got or (None, None)) == (no, title)


def test_validate_rejects_bad_formats():
    v = numbering.validate({"formats": [{"segments": SEGS, "example": "prj1-org-t1-ar-dwg-gf-1001"}],
                            "codes": {"discipline": {"se": "智能化"}}})
    assert v["formats"][0]["example"] == "PRJ1-ORG-T1-AR-DWG-GF-1001" and v["codes"]["discipline"] == {"SE": "智能化"}
    bad = [
        ({"formats": [{"segments": ["project", "floor", "number"]}]}, "不认识的段"),
        ({"formats": [{"segments": ["project", "rev?", "number"]}]}, "只能放在最后"),
        ({"formats": [{"segments": ["project", "level?", "number"]}]}, "只有版次"),
        ({"formats": [{"segments": ["project", "building", "level"]}]}, "流水号"),
        ({"formats": [{"segments": SEGS}], "codes": {"discipline": {"SE": "安防"}}}, "专业代码 SE"),
        ({"formats": [{"segments": SEGS}], "codes": {"level": {"LR": "下面那层"}}}, "看不出是第几层"),
        ({"formats": [{"segments": SEGS}], "codes": {"zone": {}}}, "四类"),
    ]
    for fmt, word in bad:
        with pytest.raises(numbering.FormatError, match=word):
            numbering.validate(fmt)
    assert numbering.decision_value("off") == "off"
    with pytest.raises(numbering.FormatError):
        numbering.decision_value("{不是 JSON")


def test_before_confirming_only_groups(tmp_path):
    proj = _receive(tmp_path)
    st = state.build(proj)
    nb = st["numbering"]
    assert nb["status"] == "guess" and nb["text"] == "项目-单位-楼栋-专业-类型-楼层-流水号（-版次）"
    assert nb["coverage"]["unmatched_examples"] == ["SUB-ZZ-GEN-XX-1009-01"]  # 分包单位的另一种写法
    iss = next(i for i in st["issues"] if i["kind"] == "图号格式")
    assert [o["label"] for o in iss["options"]] == ["按这个格式确认", "先不用"]
    by = _by(st)
    fp = by["T1 · PRJ1-ORG-T1-FP-DWG-01-1302"]["fields"]["discipline"]  # 文件夹看不出专业：用图号格式里的，把握低
    assert fp["value"] == "消防" and fp["conf"] == "low" and "待确认" in fp["src"]
    assert by["T1 · PRJ1-ORG-T1-AR-DWG-GF-1001"]["discipline"] == "建筑"  # 不再按项目代码的首字母 P 认成给排水
    assert "PRJ1-ORG-T1-AR-DWG-GF-1001-00" in by  # 确认之前：带版次后缀的还单算一张，身份、楼栋都不动
    assert "PRJ1-ORG-XX-AR-DWG-XX-01" in by
    assert by["PRJ1-ORG-XX-EL-SPEC-XX-03"]["type_kind"] == "规格书"
    power = by["T2 · PRJ1-ORG-T2-EL-DWG-01-1901"]  # 图名里看不出楼层：用图号里的楼层代码
    assert power["floors_name"] == "楼层代码 01（2F）" and power["fields"]["floors"]["conf"] == "low"


def test_confirmed_format(tmp_path):
    proj = _receive(tmp_path)
    _confirm(proj, state.build(proj), discipline={"SE": "智能化"}, level={"LR": "屋面"}, building={"XX": "公共"})
    st = state.build(proj)
    assert st["numbering"]["status"] == "confirmed" and st["numbering"]["coverage"]["unknown_codes"] == {}
    assert not [i for i in st["issues"] if i["kind"] == "图号格式"]
    by = _by(st)
    assert not [n for n in by if n.endswith("-00") or " · PRJ1" in n]  # 版次后缀拿掉了；图号本身带楼栋，不加“T1 · ”
    s = by["PRJ1-ORG-T1-AR-DWG-GF-1001"]
    assert len(s["versions"]) == 2 and s["current_rev"] == "00"  # 带不带 -00 的两份文件是同一张图
    assert s["tower"] == "T1" and s["fields"]["tower"]["conf"] == "high"
    se = by["PRJ1-ORG-T1-SE-DWG-GF-1401"]["fields"]["discipline"]
    assert se["value"] == "智能化" and se["conf"] == "high"
    assert by["PRJ1-ORG-XX-AR-DWG-XX-0001"]["tower"] == "公共"  # 前导零保留
    assert by["PRJ1-ORG-T1-AR-DET-XX-5001"]["type_kind"] == "详图大样"
    power = by["PRJ1-ORG-T2-EL-DWG-01-1901"]
    assert power["floors_name"] == "楼层代码 01（2F）" and "已确认的图号格式" in power["fields"]["floors"]["src"]
    assert "SUB-ZZ-GEN-XX-1009-01" in by  # 套不上的照旧
    add_decision(proj, "numbering", "*", "off")
    st = state.build(proj)
    assert st["numbering"]["status"] == "off" and "T1 · PRJ1-ORG-T1-AR-DWG-GF-1001" in _by(st)


def test_old_decisions_still_apply(tmp_path):
    proj = _receive(tmp_path, recognize=True)
    st = state.build(proj)
    by = _by(st)
    no = "T1 · PRJ1-ORG-T1-AR-DWG-GF-1001"  # 看图识别后图签上的图号相同：两份文件同版次、内容不同
    iss = next(i for i in st["issues"] if i["target"].get("sheet") == no and i["kind"] in ("版本冲突", "改图未升版"))
    opt = iss["options"][0]
    add_decision(proj, opt["kind"], opt["key"], opt["value"])
    add_decision(proj, "sheet_current", "PRJ1-ORG-XX-AR-DWG-XX-01", "00")
    assert "PRJ1-ORG-XX-AR-DWG-XX-01" in by
    _confirm(proj, st, discipline={"SE": "智能化"}, level={"LR": "屋面"})
    st = state.build(proj)
    by = _by(st)
    assert not [i for i in st["issues"] if i["target"].get("sheet") == "PRJ1-ORG-T1-AR-DWG-GF-1001"
                and i["kind"] in ("版本冲突", "改图未升版")]  # 确认格式以前按旧写法做的决定照样用
    assert by["PRJ1-ORG-XX-AR-DWG-XX-0001"]["fields"]["rev"]["conf"] == "confirmed"


def test_assistant_proposes_format(tmp_path):
    proj = _receive(tmp_path)
    kit = Toolkit(proj)
    ov = kit.numbering_overview()
    g = ov["groups"][0]
    assert g["parts"] == 7 and ov["current"]["status"] == "guess"
    seg = next(x for x in g["segments"] if x["position"] == 4)
    assert seg["looks_like"] == "像专业代码"
    assert any("SECURITY" in t for v in seg["values"] if v["code"] == "SE" for t in v["titles"])
    assert any(p["title"] == "DRAWING LIST" for p in ov["catalog_pages"])
    with pytest.raises(ToolError, match="不认识的段"):
        kit.propose_numbering({"formats": [{"segments": ["project", "floor", "number"]}]}, "试试")
    fmt = {"formats": [{"segments": SEGS, "example": "PRJ1-ORG-T1-SE-DWG-GF-1401"}],
           "codes": {"discipline": {"AR": "建筑", "ST": "结构", "EL": "电气", "FP": "消防", "SE": "智能化"},
                     "level": {"GF": "Ground Floor", "01": "Level 01", "LR": "屋面", "XX": "不分层"},
                     "building": {"XX": "公共"}, "type": {"DWG": "图纸", "DET": "详图大样", "SCH": "表格", "SPEC": "规格书"}},
           "why": {"discipline.SE": "例图名都是 SECURITY LAYOUT"}}
    r = kit.propose_numbering(fmt, "看了各段的取值和例图名")
    assert r["coverage"]["matched"] == 36 and r["coverage"]["unknown_codes"] == {}
    st = state.build(proj)
    assert st["numbering"]["status"] == "proposed"
    assert [o["label"] for o in next(i for i in st["issues"] if i["kind"] == "图号格式")["options"]] == ["先不用"]
    out = kit.confirm(r["proposal_id"])
    assert out["status"] == "confirmed" and "图号格式" in out["outcome"]
    st = state.build(proj)
    assert st["numbering"]["status"] == "confirmed" and st["numbering"]["format"]["why"]
    assert _by(st)["PRJ1-ORG-T1-SE-DWG-GF-1401"]["discipline"] == "智能化"


def test_server_checks_numbering_decision(tmp_path):
    proj = _receive(tmp_path)
    httpd = serve(proj, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(value):
        req = urllib.request.Request(base + "/api/decision", method="POST",
                                     data=json.dumps({"kind": "numbering", "key": "*", "value": value}).encode(),
                                     headers={"Content-Type": "application/json", "X-Bidbench-Token": httpd.app.token})
        return json.loads(urllib.request.urlopen(req).read())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            post(json.dumps({"formats": [{"segments": ["project", "floor", "number"]}]}))
        assert e.value.code == 400 and "图号格式不对" in json.loads(e.value.read())["error"]
        assert post("off")["numbering"]["status"] == "off"
    finally:
        httpd.shutdown()
