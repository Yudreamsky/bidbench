"""建模依据核对：构件清单逐条对照现行图纸（check_basis）。"""

import pymupdf
import pytest

from bidbench import export
from bidbench.assistant import Toolkit, ToolError


def _bbox(proj, page_id, text):
    path, idx, _, _ = export._page_source(proj, page_id)
    with pymupdf.open(path) as doc:
        page = doc[idx - 1]
        r = page.search_for(text)[0]
        w, h = page.rect.width, page.rect.height
        return [r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h]


def test_check_basis(demo):
    kit = Toolkit(demo)
    cur = kit.get_sheet("结施-04")["current_page_id"]  # C 版（现行）
    old = kit.get_sheet("结施-04")["versions"][0]["page_id"]  # A 版
    box = _bbox(demo, cur, "350×700")
    r = kit.check_basis([
        {"element": "3F KL3", "sheet": "结施-04", "rev": "C", "page_id": cur, "bbox": box, "value": "350×700"},
        {"element": "3F KL3 旧", "sheet": "结施-04", "rev": "A", "page_id": old, "value": "300×600"},
        {"element": "3F KL3 读错", "sheet": "结施-04", "rev": "C", "page_id": cur, "bbox": box, "value": "400×800"},
        {"element": "楼梯", "sheet": "建施-10"},
        {"element": "门窗", "sheet": "建施-08", "rev": "A", "page_id": kit.get_sheet("建施-08")["current_page_id"]},
        {"element": "门窗已改", "sheet": "建施-08", "rev": "A", "page_id": kit.get_sheet("建施-08")["current_page_id"],
         "notices_applied": ["设计变更单-012"]},
        {"element": "错页", "sheet": "结施-01", "rev": "B", "page_id": cur},
        {"sheet": "结施-04"},
    ])
    st = {x["element"]: x for x in r["items"]}
    assert st["3F KL3"]["status"] == "对得上" and "350×700" in st["3F KL3"]["checked"][0]
    old_reasons = " ".join(st["3F KL3 旧"]["reasons"])
    assert st["3F KL3 旧"]["status"] == "要复核" and "现行是 C 版" in old_reasons and "已替代" in old_reasons
    assert st["3F KL3 读错"]["status"] == "要复核" and any("没找到“400×800”" in x for x in st["3F KL3 读错"]["reasons"])
    assert st["楼梯"]["status"] == "不合格"  # 缺图不能当依据
    assert any("设计变更单-012" in x for x in st["门窗"]["reasons"])
    assert not any("设计变更单-012" in x for x in st["门窗已改"]["reasons"])
    assert st["错页"]["status"] == "不合格" and "不是 结施-01" in st["错页"]["reasons"][0]
    assert st[None]["status"] == "不合格"
    assert sum(r["summary"].values()) == 8


def test_check_basis_limits(demo):
    kit = Toolkit(demo)
    with pytest.raises(ToolError):
        kit.check_basis([])
    with pytest.raises(ToolError, match="最多"):
        kit.check_basis([{"element": "x", "sheet": "结施-04"}] * 201)
    r = kit.check_basis(["乱写", {"element": "x", "sheet": "结施-04", "rev": "C", "page_id": kit.get_sheet("结施-04")["current_page_id"],
                                 "bbox": [2, 0, 1, 1], "value": "350"}])
    assert r["items"][0]["status"] == "不合格" and any("bbox 用不了" in x for x in r["items"][1]["reasons"])
