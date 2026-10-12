"""Markdown 报告：解包报告、到图差异报告、图纸台账、缺图清单、答疑问题清单。"""

from __future__ import annotations

from pathlib import Path

from .ingest import ReceiveResult
from .state import CONF_LABEL, STATUS_LABEL
from .store import Project

KIND_LABEL = {"pdf": "PDF", "cad": "CAD 图纸", "office": "Office 文档", "image": "图片", "text": "文本", "other": "其他"}
ISSUE_LABEL = {"encrypted": "加密", "corrupt": "损坏", "no_tool": "缺少解压工具", "volume": "分卷", "symlink": "符号链接",
               "too_deep": "嵌套过深", "too_large": "超出上限", "blocked": "被拦截", "unreadable": "读不出来"}
SOURCE_LABEL = {"archive": "压缩包", "file": "单个文件", "folder": "文件夹", "files": "同时收到的几个文件"}
SAFETY_TIPS = ["不要双击运行投标文件里的程序，也不要随手点 Office 的“启用宏”。",
               "用 CAD 打开图纸前，先确认同一个文件夹里没有 acad.lsp、acaddoc.lsp、acad.fas、*.mnl 这类文件；"
               "建议在 CAD 的“选项 → 系统 → 安全”里把 SECURELOAD 设为 1 或 2，只从受信任位置加载脚本。",
               "“导出现行图纸”时，带脚本的 PDF 会自动去掉脚本后再导出。"]


def receive_report(proj: Project, r: ReceiveResult, security: dict | None = None) -> Path:

    kinds = "、".join(f"{KIND_LABEL.get(k, k)} {n} 个" for k, n in sorted(r.by_kind.items())) or "无"
    lines = [f"# 批次 #{r.seq} 解包报告：{r.name}", "",
             f"收图来源：{SOURCE_LABEL.get(r.source, r.source)}。共收进 {r.files} 个文件：{kinds}。"
             f"新增 PDF 页面 {r.new_pages} 页（与已收文件内容相同的不重复计算）。", ""]
    if r.skipped:
        lines += [f"没有收的系统自动生成文件 {len(r.skipped)} 个（缩略图缓存、Office 临时锁文件等，不是图纸）："
                  + "、".join(r.skipped[:10]) + ("等" if len(r.skipped) > 10 else "") + "。", ""]
    if r.duplicates:
        lines += [f"本批次内有 {r.duplicates} 个文件与本批其他位置的文件完全相同，只保存一份，出处都已记录。", ""]
    if r.nested_archives:
        lines += ["## 内层压缩包（已自动展开）", ""] + [f"- {p}" for p in r.nested_archives] + [""]
    issues = [i for i in r.issues if i["kind"] != "blocked"]  # 被杀毒软件拦下的列在下面的文件安全检查里
    if issues:
        lines += ["## 需要处理", "", "下面的文件没能完整收进来，里面可能藏着缺失的图纸。", "",
                  "| 问题 | 位置 | 说明 |", "| --- | --- | --- |"]
        lines += [f"| {ISSUE_LABEL.get(i['kind'], i['kind'])} | {i['path']} | {i['detail']} |" for i in issues] + [""]
    else:
        lines += ["没有解不开或读不出来的文件。", ""]
    lines += security_section(security)
    out = proj.path("reports", f"批次{r.seq:02d}-解包报告.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def scan_line(s: dict) -> str:
    where = f"批次 #{s['batch_seq']}" if s["batch_seq"] else "全部文件"
    if not s["ok"]:
        return f"{s['scanned_at'][:16].replace('T', ' ')} {s['engine']} 扫描{where}没有完成：{s['message']}"
    res = f"发现 {s['threats']} 个有问题的文件" if s["threats"] else "未发现病毒"
    return f"{s['scanned_at'][:16].replace('T', ' ')} {s['engine']} 扫描{where}的 {s['files']} 个文件，{res}"


def security_section(security: dict | None) -> list[str]:
    if security is None:
        return []
    lines = ["## 文件安全检查", ""]
    scans = security.get("scans") or []
    lines += [f"- 杀毒扫描：{scan_line(s)}。" for s in scans] or [
        "- 杀毒扫描：本机没有找到可用的杀毒软件（Windows 自带 Defender；其他系统可装 ClamAV），只做了静态检查。"
        if security.get("av") is None else "- 杀毒扫描：已跳过（--no-av）。"]
    files = security.get("files") or []
    if not files:
        return lines + ["- 静态检查：没有发现 CAD 自动加载脚本、可执行文件、宏或带脚本的 PDF。", ""]
    lines += ["", "| 风险 | 文件 | 发现 |", "| --- | --- | --- |"]
    lines += [f"| {LEVEL.get(f['level'], f['level'])} | {f['path']} | {'；'.join(f['details'])} |" for f in files]
    return lines + ["", *[f"- {t}" for t in SAFETY_TIPS], ""]


LEVEL = {"high": "高", "medium": "中", "low": "低"}


def security_report(state: dict) -> str:
    sec = state.get("security") or {}
    lines = [f"# 文件安全检查（截至批次 #{state['as_of']}）", ""]
    scans = sec.get("scans") or []
    lines += ["## 杀毒扫描", ""] + ([f"- {scan_line(s)}" for s in scans] or ["- 还没有做过杀毒扫描。"]) + [""]
    files = sec.get("files") or []
    lines += ["## 风险文件", ""]
    if not files:
        lines += ["没有发现风险文件。", ""]
    else:
        from .state import RISK_DECISION
        lines += ["| 风险 | 文件 | 发现 | 处理 | 位置 |", "| --- | --- | --- | --- | --- |"]
        for f in files:
            lines.append(f"| {f['level_word']} | {f['name']} | {'；'.join(f['findings'])} | "
                         f"{RISK_DECISION.get(f['decision'], '待确认' if f['level'] != 'low' else '—')} | {'<br>'.join(f['paths'])} |")
        lines.append("")
    lines += ["## 注意", ""] + [f"- {t}" for t in SAFETY_TIPS] + [
        "- 静态检查只看文件名和内容特征，不能代替杀毒软件；DWG 文件本身不能运行代码，风险在同目录的脚本和插件。"]
    return "\n".join(lines) + "\n"


def diff_report(state: dict) -> str:
    d = state.get("diff") or {}
    b = next((x for x in state["batches"] if x["seq"] == state["as_of"]), None)
    if not b:
        return ""
    lines = [f"# 批次 #{b['seq']} 到图差异报告", "",
             f"收图日期 {b['received_at']}，来源 {b['archive']}。判断为：{'、'.join(b['kinds'])}（把握：{b['confidence']}）。", ""]

    def sec(title, items):
        if items:
            lines.extend([f"## {title}", ""] + [f"- {x}" for x in items] + [""])

    sec("升版", d.get("upgraded"))
    sec("新增图纸", [x + ("（已核销缺图清单）" if x in d.get("resolved_missing", []) else "") for x in d.get("added", [])])
    sec("新发现的缺图", d.get("new_missing"))
    sec("新出现的版本冲突", d.get("new_conflicts"))
    sec("新出现的事实冲突", d.get("new_fact_conflicts"))
    sec("作废还是漏发，待确认", d.get("pending_void"))
    sec("新发现的楼层缺口", d.get("floor_gaps"))
    sec("事实变化", d.get("fact_changes"))
    sec("受影响的建模任务", d.get("baselines"))
    notices = [n for n in state.get("notices", []) if n["batch"] == b["seq"]]
    sec("变更文件", [f"{n['id']} {n['title'] or ''}：影响 {'、'.join(n['affects']) or '（未识别出图号）'}" for n in notices])
    if b["issues"]:
        sec("解包问题", [f"{ISSUE_LABEL.get(i['kind'], i['kind'])}：{i['path']}" for i in b["issues"]])
    if len(lines) <= 4:
        lines.append("与上一批相比没有变化。")
    return "\n".join(lines) + "\n"


def register_report(state: dict) -> str:
    lines = [f"# 图纸台账（截至批次 #{state['as_of']}）", "",
             "| 专业 | 图号 | 图名 | 现行版次 | 状态 | 出处 |", "| --- | --- | --- | --- | --- | --- |"]
    for s in state["sheets"]:
        lines.append(f"| {s['discipline']} | {s['no']} | {s['title'] or ''} | {s['current_rev'] or '—'} | "
                     f"{STATUS_LABEL[s['status']]} | {(s['paths'] or [''])[0]} |")
    return "\n".join(lines) + "\n"


def missing_report(state: dict) -> str:
    rows = [s for s in state["sheets"] if s["status"] == "missing"]
    lines = [f"# 缺图清单（截至批次 #{state['as_of']}）", ""]
    if rows:
        lines += ["| 图号 | 图名 | 可靠程度 | 依据 |", "| --- | --- | --- | --- |"]
        lines += [f"| {s['no']} | {s['title'] or ''} | {s.get('missing_level', '')} | {'；'.join(s['notes'])} |" for s in rows]
    else:
        lines.append("目前没有发现缺图号的图纸。")
    lines += floor_section(state)
    ext = [i for i in state["issues"] if i["kind"] == "解包问题"]
    if ext:
        lines += ["", "另有以下文件没能解开，里面可能还有图纸：", ""] + [f"- {i['title']}：{i['detail']}" for i in ext]
    blocked = [f for f in (state.get("security") or {}).get("files", []) if f.get("blocked")]
    if blocked:
        lines += ["", "以下文件收图时被本机杀毒软件拦下，没有收进项目；如果是图纸，需要发图方重新发一份干净的：", ""]
        lines += [f"- {f['name']}：{(f['paths'] or [''])[0]}" for f in blocked]
    return "\n".join(lines) + "\n"


FLOOR_STATUS = {"ok": "完整", "no_gap": "已出现楼层之间不缺", "missing": "有缺层", "accepted": "缺层已确认不需要",
                "unchecked": "无法检查"}


def floor_section(state: dict) -> list[str]:
    rows = state.get("floor_coverage") or []
    if not rows:
        return []
    out = ["", "## 楼层覆盖", "", "| 图类 | 结果 | 已覆盖 | 缺少 | 依据 |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        have = "；".join(f"{c['floors']}（{c['sheets'][0]}{'，待确认' if c.get('tentative') else ''}）" for c in r["covered"]) or "—"
        basis = "；".join(x for x in [r["basis"], *r.get("remarks", [])] if x)
        out.append(f"| {r['group']} | {FLOOR_STATUS[r['status']]} | {have} | {r['missing_text'] or '—'} | {basis} |")
    return out


def qa_report(state: dict) -> str:
    """答疑问题清单：高可靠的缺图、已确认要图的缺图、版本冲突、真实的事实矛盾。"""
    items = []
    for s in state["sheets"]:
        if s["status"] == "missing" and (s.get("missing_level") == "高" or s.get("confirmed")):
            items.append(f"招标图纸中缺少 {s['no']} {s['title'] or ''}（{'；'.join(s['notes'])}），请补发。")
    for f in state.get("floor_coverage") or []:
        if f["status"] == "missing" and f.get("confirmed"):
            items.append(f"{f['group']}缺少 {f['missing_text']} 的图纸（{f['basis']}），请补发或说明。")
    for i in state["issues"]:
        if i["kind"] == "版本冲突":
            items.append(f"{i['title']}：{i['detail']}请明确以哪一份为准。")
        if i["kind"] == "事实冲突":
            items.append(f"{i['title']}：{i['detail']}请澄清。")
    for f in (state.get("security") or {}).get("files", []):
        if f["decision"] == "dangerous":
            items.append(f"收到的文件 {f['name']}（{f['paths'][0] if f['paths'] else ''}）{'；'.join(f['findings'])}。"
                         "请核实来源，并重新提供不含程序、脚本和宏的文件。")
    lines = [f"# 答疑问题清单（草稿，截至批次 #{state['as_of']}）", "",
             "由系统根据缺图和冲突自动整理，提交前请人工核对措辞。", ""]
    lines += [f"{n}. {t}" for n, t in enumerate(items, 1)] or ["暂无需要答疑的问题。"]
    return "\n".join(lines) + "\n"


def facts_report(state: dict) -> str:
    lines = [f"# 项目事实（截至批次 #{state['as_of']}）", ""]
    for g in state["facts"]["groups"]:
        lines += [f"## {g['label']}", "", "| 事实 | 值 | 置信度 | 出处 | 备注 |", "| --- | --- | --- | --- | --- |"]
        for f in g["facts"]:
            lines.append(f"| {f['name']} | {f['value']} | {CONF_LABEL[f['conf']]} | {'、'.join(f['sources'])} | {f['note'] or ''} |")
        lines.append("")
    return "\n".join(lines)


def write_all(proj: Project, state: dict, subdir: str | None = None) -> list[Path]:
    """写全部报告。还原历史状态时传 subdir，避免覆盖最新报告。"""
    out = []
    base = ("reports", subdir) if subdir else ("reports",)
    for name, fn in (("图纸台账", register_report), ("缺图清单", missing_report), ("答疑问题清单", qa_report),
                     ("项目事实", facts_report), ("文件安全检查", security_report)):
        p = proj.path(*base, f"{name}.md")
        p.write_text(fn(state), encoding="utf-8")
        out.append(p)
    if state["as_of"]:
        p = proj.path(*base, f"批次{state['as_of']:02d}-到图差异报告.md")
        p.write_text(diff_report(state), encoding="utf-8")
        out.append(p)
    return out
