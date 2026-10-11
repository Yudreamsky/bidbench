"""模拟执行器：只用 PDF 文字层 + 规则抽取，代替真正的多模态识别。

用途：在接入 Codex 或内置多模态 agent 之前，让整条流水线能跑通。
- 对 CAD 直接导出、文字层完整的 PDF，能抽出图签和部分内容；
- 对扫描件无能为力，只会输出空字段并登记问题，绝不编造。
输出与真实执行器完全同一格式，所以替换执行器时其余环节不用改。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..recognition import page_markdown as to_markdown

EXECUTOR = "simulated"
MODEL = "rules-text-layer"
VERSION = "0.2"

DRAWING_NO = r"(?:[一-龥]{0,3}施|[A-Z]{1,3})\s*[-－—_]\s*\d{1,3}[A-Za-z]?"
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

DISCIPLINE_BY_PREFIX = [
    ("建施", "建筑"), ("建", "建筑"), ("JS", "建筑"), ("A", "建筑"),
    ("结施", "结构"), ("结", "结构"), ("GS", "结构"), ("S", "结构"),
    ("水施", "给排水"), ("水", "给排水"), ("P", "给排水"),
    ("暖施", "暖通"), ("暖", "暖通"), ("N", "暖通"), ("M", "暖通"),
    ("电施", "电气"), ("电", "电气"), ("D", "电气"), ("E", "电气"),
]

KV_RULES = [
    ("地上层数", re.compile(r"地上\s*(\d+)\s*层"), "层"),
    ("地下层数", re.compile(r"地下\s*(\d+)\s*层"), "层"),
    ("建筑高度", re.compile(r"建筑高度[\s:：为]*([\d.]+)\s*m"), "m"),
    ("屋面标高", re.compile(r"屋面标高\s*([\d.]+)"), "m"),
    ("结构形式", re.compile(r"结构(?:形式|类型)[\s:：为]*([一-龥\-－—]{2,12})"), None),
    ("抗震设防烈度", re.compile(r"抗震设防烈度[\s:：为]*(\d)\s*度"), "度"),
    ("混凝土强度等级", re.compile(r"混凝土强度等级[\s:：为]*(C\d{2})"), None),
    ("总建筑面积", re.compile(r"总建筑面积[\s:：为]*([\d.,]+)\s*(?:m2|㎡|平方米)"), "m2"),
]
FLOOR_HEIGHT = re.compile(r"(\d+F(?:\s*[~～\-]\s*\d+F)?)\s*层高\s*([\d.]+)\s*m")
BEAM = re.compile(r"(?<![A-Za-z])(KL\d+|L\d+|WKL\d+)\s*(?:\(\d+\))?\s*(?:截面)?\s*(\d{3,4}\s*[×xX*]\s*\d{3,4})")
WALL = re.compile(r"(?<![A-Za-z])(Q\d+)\s*墙厚\s*(\d{3})")
STD_NAMED = re.compile(r"《([^》]{2,30})》\s*((?:GB|JGJ|CECS|DB)\s*/?\s*T?\s*\d{3,6}-\d{4}(?:（\d{4}年版）)?)")
STD_BARE = re.compile(r"((?:GB|JGJ)\s*/?\s*T?\s*\d{3,6}-\d{4}(?:（\d{4}年版）)?)")
REF_INDEX = re.compile(rf"([{CIRCLED}]|\d{{1,2}})\s*/\s*({DRAWING_NO})")
REF_TEXT = re.compile(rf"(?:详见|见|参见|对应|修改)\s*({DRAWING_NO})")
REF_ATLAS = re.compile(r"(?:详见|见|参见)\s*(\d{2}[A-Z]{1,3}\d{2,4}(?:-\d)?)\s*(?:图集)?")
CATALOG_ROW = re.compile(rf"^\s*({DRAWING_NO})\s+(\S.*?)\s+([A-Z]|\d{{1,2}})\s*$")


def _f(value, conf=0.9):
    if value is None:
        return {"value": None, "source": None, "confidence": None}
    return {"value": value, "source": "text_layer", "confidence": conf}


def _clean_no(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _discipline(no: str | None) -> str | None:
    if not no:
        return None
    for prefix, name in DISCIPLINE_BY_PREFIX:
        if no.upper().startswith(prefix.upper()):
            return name
    return None


def _labelled(lines, label: str) -> tuple[str | None, list | None]:
    """取“标签 值”形式的图签字段。优先图签常见区域（右下角），否则取最后一处，避开目录表头等。"""
    pat = re.compile(rf"^\s*{label}[\s:：]+(.+?)\s*$")
    hits = []
    for ln in lines:
        m = pat.match(ln["text"])
        if m and not re.search(r"图名|版次|图号", m.group(1)):
            hits.append((m.group(1).strip(), ln["bbox"]))
    if not hits:
        return None, None
    in_corner = [h for h in hits if h[1][0] >= 0.55 and h[1][1] >= 0.55]
    return (in_corner or hits)[-1]


def recognize_page(item: dict, lines: list[dict]) -> dict:
    texts = [ln["text"] for ln in lines]
    has_text = bool(texts)
    issues = []
    no, tb_bbox = _labelled(lines, "图号")
    title, _ = _labelled(lines, "图名")
    rev, _ = _labelled(lines, "版次")
    date, _ = _labelled(lines, "日期")
    scale, _ = _labelled(lines, "比例")
    project, _ = _labelled(lines, "工程名称")
    org, _ = _labelled(lines, "设计单位")
    no = _clean_no(no) if no else None

    history = []
    for ln in lines:
        m = re.match(r"^\s*修改记录[\s:：]*([A-Za-z0-9]+)\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s*$", ln["text"])
        if m:
            history.append({"revision": m.group(1), "date": m.group(2), "description": m.group(3),
                            "source": "text_layer", "confidence": 0.9})

    head = (title or "") + " " + " ".join(texts[:3])
    if "目录" in (title or ""):
        kind = "图纸目录"
    elif re.search(r"变更", head):
        kind = "变更单"
    elif re.search(r"答疑|澄清", head):
        kind = "答疑或澄清"
    elif re.search(r"说明", title or ""):
        kind = "设计说明"
    elif not has_text:
        kind = "其他"
    else:
        kind = "图纸"

    refs, seen = [], set()
    for ln in lines:
        t = ln["text"]
        if re.match(r"^\s*图号", t):
            continue
        for m in REF_INDEX.finditer(t):
            target = _clean_no(m.group(2))
            if target != no and ("idx", m.group(0)) not in seen:
                seen.add(("idx", m.group(0)))
                refs.append({"raw_text": m.group(0), "target_drawing_no": target, "ref_type": "详图索引",
                             "bbox": ln["bbox"], "source": "text_layer", "confidence": 0.85})
        for m in REF_TEXT.finditer(t):
            target = _clean_no(m.group(1))
            if target != no and ("txt", target, t) not in seen and kind != "图纸目录":
                seen.add(("txt", target, t))
                refs.append({"raw_text": m.group(0), "target_drawing_no": target,
                             "ref_type": "大样引用" if "大样" in t else "文字引用",
                             "bbox": ln["bbox"], "source": "text_layer", "confidence": 0.85})
        for m in REF_ATLAS.finditer(t):
            refs.append({"raw_text": m.group(0), "target_drawing_no": None, "ref_type": "图集引用",
                         "bbox": ln["bbox"], "source": "text_layer", "confidence": 0.8})

    catalog = []
    if kind == "图纸目录":
        for ln in lines:
            m = CATALOG_ROW.match(ln["text"])
            if m and not ln["text"].strip().startswith("图号"):
                catalog.append({"drawing_no": _clean_no(m.group(1)), "drawing_title": m.group(2),
                                "revision": m.group(3), "source": "text_layer", "confidence": 0.9})

    kvs, notes, standards, std_seen = [], [], [], set()

    def kv(name, value, unit, raw, bbox):
        kvs.append({"name": name, "value": value, "unit": unit, "raw_text": raw, "bbox": bbox,
                    "source": "text_layer", "confidence": 0.9})

    for ln in lines:
        t = ln["text"]
        for name, pat, unit in KV_RULES:
            m = pat.search(t)
            if m:
                kv(name, m.group(1), unit, m.group(0), ln["bbox"])
        for m in FLOOR_HEIGHT.finditer(t):
            floor = re.sub(r"\s+", "", m.group(1)).replace("～", "~").replace("-", "~")
            kv(f"{floor} 层高", m.group(2), "m", m.group(0), ln["bbox"])
        for m in BEAM.finditer(t):
            kv(f"{m.group(1)} 截面（{title or no or '未知图'}）", re.sub(r"\s+", "", m.group(2)).replace("x", "×").replace("X", "×").replace("*", "×"),
               "mm", m.group(0), ln["bbox"])
        for m in WALL.finditer(t):
            kv(f"{m.group(1)} 墙厚", m.group(2), "mm", m.group(0), ln["bbox"])
        named = list(STD_NAMED.finditer(t))
        for m in named:
            code = re.sub(r"\s+", " ", m.group(2)).strip()
            if code not in std_seen:
                std_seen.add(code)
                standards.append({"code": code, "name": m.group(1), "source": "text_layer", "confidence": 0.9})
        if not named:
            for m in STD_BARE.finditer(t):
                code = re.sub(r"\s+", " ", m.group(1)).strip()
                if code not in std_seen:
                    std_seen.add(code)
                    standards.append({"code": code, "name": None, "source": "text_layer", "confidence": 0.8})
        m = re.match(r"^\s*(\d+(?:\.\d+)*)[\s、.．]+(.{4,})$", t)
        if m and kind in ("设计说明", "变更单", "答疑或澄清", "图纸"):
            notes.append({"label": m.group(1), "text": m.group(2), "bbox": ln["bbox"],
                          "source": "text_layer", "confidence": 0.9})

    if not has_text:
        issues.append({"type": "其他", "description": "页面没有文字层（可能是扫描件），模拟执行器无法识别，需要多模态执行器。"})
    elif not no:
        issues.append({"type": "图签缺失", "description": "文字层中没有找到“图号”字段。"})

    summary = f"{title}。" if title else ("无文字层页面。" if not has_text else "未能从文字层判断图纸内容。")
    if kind == "图纸目录":
        summary = f"{title or '图纸目录'}，共 {len(catalog)} 行。"

    return {
        "schema_version": "0.1",
        "page_id": item["page_id"],
        "source": item["source"],
        "run": {"executor": EXECUTOR, "model": MODEL, "instruction_version": VERSION,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "page_kind": kind,
        "title_block": {
            "drawing_no": _f(no), "drawing_title": _f(title), "discipline": _f(_discipline(no), 0.7),
            "revision": _f(rev), "issue_date": _f(date), "scale": _f(scale),
            "project_name": _f(project), "designer_org": _f(org),
            "revision_history": history, "bbox": tb_bbox,
        },
        "references": refs,
        "catalog_entries": catalog,
        "content": {"summary": summary, "notes": notes, "tables": [], "key_values": kvs, "standards": standards},
        "issues": issues,
    }


def run(task_dir: Path) -> int:
    task_dir = Path(task_dir)
    manifest = json.loads((task_dir / "manifest_in.json").read_text(encoding="utf-8"))
    out = task_dir / "results" / "pages"
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for item in manifest["pages"]:
        target = out / f"{item['page_id']}.json"
        if target.exists():
            continue  # 支持续跑
        lines = json.loads((task_dir / item["text_lines"]).read_text(encoding="utf-8"))["lines"]
        r = recognize_page(item, lines)
        target.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / f"{item['page_id']}.md").write_text(to_markdown(r), encoding="utf-8")
        n += 1
    (task_dir / "results" / "run_log.md").write_text(
        f"# 运行记录\n\n执行器：{EXECUTOR}（{MODEL}）\n\n本次新识别 {n} 页。模拟执行器只读文字层，扫描件会留空并登记问题。\n",
        encoding="utf-8",
    )
    return n
