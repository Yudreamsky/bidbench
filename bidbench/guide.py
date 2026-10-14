"""指引：问题中心最上面的“下一步”、每个问题的“怎么选？”、做过的决定写成人话。

都是给人看的说明，按派生状态现算，不落库。新加一种问题（state._issue 的 id 前缀）时，在 HELP 里写上它的说明，
tests/test_guide.py 会检查每种问题都有。
"""

from __future__ import annotations

import json
import zlib
from collections import Counter

from . import floors, numbering, towers

# 每种问题按 id 的前缀（冒号前面那段）找说明：是什么、怎么看、拿不准怎么选、选了以后（没有选项的问题不写“拿不准”）
HELP: dict[str, dict[str, str]] = {
    "missing": {
        "what": "目录列出了、别的图引用了，或者编号中间断了号，可是收到的文件里都没有这张图。",
        "look": "图纸卡片的“引用关系”里能看到是哪张图引用的、哪份目录列出的；再在图谱里搜一下图号，看是不是写法不同的同一张图。",
        "unsure": "选「列入答疑」，答疑清单里会写上请招标方补发。",
        "effect": "列入答疑：问题留着，标“已列入答疑”，写进答疑清单。确认不需要：问题消失，不再算缺图。",
    },
    "floor": {
        "what": "同一类图按楼层排下来，中间缺了几层（比如有 1~5F、7~10F 的梁平面，没有 6F）。",
        "look": "看下面“楼层覆盖”里已有哪些层：缺的层可能被标准层图覆盖了，也可能这栋楼本来就没有那一层。",
        "unsure": "选「列入答疑」。",
        "effect": "列入答疑：写进答疑清单，请招标方补发或说明。确认不需要：这一组楼层算完整，不再提醒。",
    },
    "floor_unknown": {
        "what": "这张标准层图的图名和说明里都没写适用哪几层，没法判断它覆盖了哪些楼层。",
        "look": "打开原图，看图名下面或说明里有没有写“适用于 X~Y 层”。",
        "effect": "这条只是提醒，不用选；查楼层覆盖时这张图先放一边。",
    },
    "rev_dup": {
        "what": "同一张图收到几份，版次一样、内容不一样，日期也分不出先后，软件定不了哪份是现行的。",
        "look": "在图纸卡片的“版本链”里逐份点“原图”，对比修改云线、修改记录和图签日期。",
        "unsure": "先不选。没定的版本冲突会自动写进答疑清单，请发图方明确；定下来之前别拿这张图建模。",
        "effect": "选中的那份成为现行版，冲突消失，也不再写进答疑清单。",
    },
    "rev_unclear": {
        "what": "几份文件的版次写法不一样（比如 B 和 2），或者都没写版次和日期，比不出新旧。软件先按其中一份暂定。",
        "look": "在图纸卡片的“版本链”里打开各份原图，看图签和修改记录里的版次、日期。",
        "unsure": "先不选。没定的会写进答疑清单，请发图方明确以哪一版为准。",
        "effect": "你选的那一版成为现行版，问题消失。",
    },
    "rev_date": {
        "what": "版次高的那份，日期反而比版次低的早。可能是日期写错了，也可能高版次已经作废。",
        "look": "对比两份的修改记录：高版次改了什么，日期像不像笔误。",
        "unsure": "先不选：软件暂按版次高的算，问题会写进答疑清单。",
        "effect": "你选的那一版成为现行版，问题消失。",
    },
    "rev_same": {
        "what": "同一张图收到几份，版次一样，内容却不同。软件按日期，把晚的那份当现行版。",
        "look": "在图纸卡片的“版本链”里逐份点“原图”，看改了哪里。",
        "unsure": "选「列入答疑」，请发图方确认以哪份为准，或者升版重发。",
        "effect": "改用另一份：那份成为现行版。列入答疑：写进答疑清单。知道了：问题消失，按日期晚的算。",
    },
    "rev_older": {
        "what": "后收到的文件反而是旧版次。软件按版次，仍以新版次为现行。",
        "look": "看这一批的发图说明或邮件：是不是新版作废了、退回到旧版。",
        "unsure": "先不选，问题会留着提醒你；问清楚以后再选。",
        "effect": "你选的那一版成为现行版，提醒消失。",
    },
    "void": {
        "what": "这一批像是整套重发，里面却没有这张以前收到过的图：可能作废了，也可能漏发了。",
        "look": "看这一批的发图说明，或者新目录里还有没有这张图。",
        "unsure": "先不选。没定之前这张图照常算现行，只是标成“作废还是漏发”。",
        "effect": "确认已作废：不再算现行图纸，还引用它的图会被提醒。仍然有效：照常算现行图纸。",
    },
    "ref_void": {
        "what": "一张确认作废的图，还被别的图引用着。",
        "look": "打开引用它的图，看引用的是什么：可能引用它的图也该改，也可能当初作废的判断有误。",
        "effect": "这条只是提醒，不用选。作废选错了，到下面“做过的决定”里撤回那一条。",
    },
    "catalog_newer": {
        "what": "图纸目录上写的版次，比收到的这张图新：可能还缺新版，手上这份可能已经过时。",
        "look": "核对目录页上这一行和这张图图签上的版次。",
        "unsure": "选「列入答疑」，请招标方补发新版；在这之前别拿这张图建模。",
        "effect": "列入答疑：写进答疑清单。目录笔误：问题消失，以收到的版本为准。",
    },
    "catalog_behind": {
        "what": "目录上有几张图的版次比收到的旧，目录没跟着图纸更新。",
        "look": "一般以收到的最新版为准；只有要拿目录当合同依据时才需要追问。",
        "unsure": "选「知道了，以收到的版本为准」。",
        "effect": "列入答疑：写进答疑清单，请发图方更新目录。知道了：问题消失；以后又有图升版时会再提醒。",
    },
    "fact": {
        "what": "同一个数（比如地上层数、建筑高度）在不同图纸上写得不一样。",
        "look": "看“证据”里每个数出自哪张图；带“含……”“不计……”这类说明的，可能只是统计口径不同。",
        "unsure": "先不选。没定的事实冲突会写进答疑清单，请设计方澄清。",
        "effect": "采用的值标成“人工确认”，冲突消失，也不再写进答疑清单。",
    },
    "name": {
        "what": "文件名里写的图号或版次，和图签上识别出来的对不上。台账以图签为准。",
        "look": "打开原图看图签。也可能是识别看错了。",
        "unsure": "图签确实这么写、只是文件名起错了：选「知道了，以图签为准」。分不清哪个对：选「列入答疑」。",
        "effect": "列入答疑：写进答疑清单。知道了：问题消失。",
    },
    "tower": {
        "what": "几栋楼都有这个图号，这份文件的文件夹、文件名和图上都看不出是哪栋的。",
        "look": "打开原图看图签里的子项、楼栋名称，或者看它和哪些文件放在一起。",
        "unsure": "先不选。没定之前它单独列着，不跟哪栋的版本比。",
        "effect": "这份文件归到你选的楼栋，按那栋的版本、缺图算；选“几栋共用”，每栋都能用它。",
    },
    "tower_alias": {
        "what": "文件夹或图上有的写这个名字，有的写那个名字，软件猜可能是同一栋楼。",
        "look": "看总图或项目说明里楼栋怎么叫（比如“1#楼”和“T1”）。",
        "unsure": "选「不是同一栋」：分开算顶多多出几条缺图提醒，不会把两栋楼的图混在一起比版本。",
        "effect": "是同一栋：并成一栋，缺图、版本按一栋算。不是同一栋：分开算。",
    },
    "floor_base": {
        "what": "英式图纸地面那层叫 Ground（Level 00），上一层才是 Level 01；国内和美式图纸 Level 01 就是首层。",
        "look": "打开一张首层平面或剖面，看 ±0.000 那层图名写的是 Ground 还是 Level 01。",
        "unsure": "选排在前面的那个，是软件按这个项目的图名猜的。",
        "effect": "只影响楼层换算：同层图纸、楼层覆盖按你选的数；不影响缺图、版本。",
    },
    "numbering": {
        "what": "图号每一段代表什么：项目、楼栋、专业、楼层、流水号、版次……确认以后才用它分楼栋、认专业和楼层，"
                "带版次后缀的文件并成一张图。",
        "look": "看上面“图号格式”卡片：每段下面是例子；码表里打“？”的是还不知道意思的代码。",
        "unsure": "让 AI 助手认（卡片上的「让 AI 助手认」）：它看文件名、目录页和图签文字，给一份带依据的格式；"
                  "它已经提议了的，核对卡片上打“？”的地方再确认。也可以先放着：没确认之前只用来分组显示，不影响缺图、版本。",
        "effect": "确认：按这个格式拆图号。先不用：按 ISO 英国附录的顺序读图号。",
    },
    "numbering_new": {
        "what": "确认过的图号格式，有一些新收的图号套不上，或者码表里有代码还不知道意思。",
        "look": "看下面“图号格式”一节里套不上的例子。",
        "effect": "这条只是提醒，不用选。可以点“图号格式”一节里的「让 AI 助手补充」，补完再确认一次。",
    },
    "risk": {
        "what": "收到的文件里有可能自动运行的东西：CAD 自动加载脚本、可执行程序、宏、带脚本的 PDF，或者被杀毒软件拦下了。",
        "look": "不要双击打开。看文件名和位置：图纸包里出现 .exe、.bat、.lsp、.vlx 这类文件，一般都不是图纸。",
        "unsure": "选“列入答疑”的那个：答疑清单里会请发图方核实，重新提供干净的文件。",
        "effect": "确认安全：不再提示。确认有风险：写进答疑清单。",
    },
    "extract": {
        "what": "有压缩包没解开：要密码、损坏、分卷不全，或者缺少解压工具。里面可能藏着缺的图。",
        "look": "看“位置”写的是哪个压缩包。要密码的问发图方要；损坏、分卷不全的请发图方重发。",
        "effect": "这条不用选。拿到密码或新文件后重新「收图」（密码填在“压缩包密码”里），里面的图会收进新的一批。",
    },
    "dwg": {
        "what": "有个 DWG（或 DXF）读不出图签和文字：文件可能损坏、版本太新，或者太大读超时了。",
        "look": "用 CAD 打开看看能不能打开。打不开的请发图方重发；能打开的，可以另存为 2018 或更早的格式再收一次。",
        "effect": "这条不用选。读不出的文件按文件名暂定，不参与缺图、版本判断；有同图号的 PDF 时照样以 PDF 为准。",
    },
    "baseline": {
        "what": "锁定基线以后，基线里有图升版或者作废了：照这份基线建模，可能用的是旧图。",
        "look": "看哪些图从哪一版升到了哪一版，告诉做模型的人换图。",
        "effect": "这条不用选。换完图，可以在下面“建模任务的图纸基线”里重新锁定一份。",
    },
    "unrecognized": {
        "what": "这些页还没看图识别，现在是按文件名暂定的（图谱里的虚线框），不拿来判断缺图和版本。",
        "look": "点上方「识别」先估算要多久；也可以在图谱里点一张图或一个框，只识别要用的。",
        "effect": "这条不用选。识别完才能查缺图、比版本、对数字。",
    },
    "newer_files": {
        "what": "有文件的文件名写着更新的版次，可是还没看图识别，现行版还是按旧的算。",
        "look": "点开这些图，在“还没看图识别的同图号文件”里看图识别。",
        "effect": "这条不用选。识别完再确认现行版，在这之前别拿旧版建模。",
    },
    "pending_refs": {
        "what": "有些被引用、被目录列出的图号，出现在还没识别的文件名里，多半只是还没识别到。",
        "look": "识别完这些页面再看。",
        "effect": "这条不用选。识别完还找不到的，才会算缺图。",
    },
    "weak_rec": {
        "what": "有些页识别得不完整：扫描件、字迹不清、图签不全，或者没读出图号。",
        "look": "点上方「识别」，范围选“识别不完整的页面”，用强档复核。",
        "effect": "这条不用选。没读出图号的页先按文件名暂定（图谱里的虚线框）。",
    },
}


def help_key(issue_id: str) -> str:
    return issue_id.split(":", 1)[0]


def help_for(issues: list) -> dict:
    """这次的问题里出现的各种问题的说明。"""
    return {k: HELP[k] for k in sorted({help_key(i["id"]) for i in issues}) if k in HELP}


# ---------------- 下一步 ----------------

STAGES = (("receive", "收图"), ("setup", "认写法"), ("recognize", "看图识别"), ("issues", "处理问题"), ("export", "导出"))
SETUP_KINDS = {"图号格式", "塔楼不明", "塔楼叫法", "楼层数法"}


def _open(i: dict) -> bool:
    """还要人处理的：严重、提醒里还没列入答疑的。"""
    return i["level"] in ("crit", "warn") and not i.get("confirmed")


def _sig(ids) -> str:
    """这一步牵涉哪些问题的指纹：跳过一步之后，问题变了就重新显示。"""
    return format(zlib.crc32("\n".join(sorted(ids)).encode("utf-8")), "08x")


def _kinds(issues: list) -> str:
    return "、".join(f"{k} {n}" for k, n in Counter(i["kind"] for i in issues).most_common())


def next_steps(st: dict) -> dict:
    """现在该做什么：按“收图 → 认写法 → 看图识别 → 处理问题 → 导出”排好的几件事，第一件就是下一步。
    每件事带 action（界面上按哪个按钮）、first（要处理的第一个问题）和 sig（界面上“先做别的”记的是它）。
    ack：只要看一下、没什么可选的（压缩包没解开、基线过期这类）：界面上的按钮是“知道了”，点过就算做完。
    stages 的 done：前面的步骤都做完、这一步也没事了才打勾（界面上点过“知道了”的也算做完）。"""
    issues = st.get("issues") or []
    batches = bool(st.get("batches"))
    tasks: list[dict] = []

    def add(stage, tid, title, text, action, about=(), skip=True, ack=False):
        ids = [x["id"] if isinstance(x, dict) else str(x) for x in about]
        tasks.append({"id": tid, "stage": stage, "title": title, "text": text, "action": action,
                      "first": ids[0] if ids and isinstance(about[0], dict) else None,
                      "sig": _sig(ids), "skip": skip, "ack": ack})

    risky = [i for i in issues if i["kind"] == "风险文件" and not i.get("confirmed")]
    if risky:
        add("receive", "risk", f"先确认 {len(risky)} 个风险文件",
            "收到的文件里有可能自动运行的脚本、程序或宏。确认之前不要双击打开，也不要和图纸放在一起用 CAD 打开。"
            "每个文件下面有“怎么选？”；拿不准就选列入答疑的那个，请发图方核实。", "issue", risky, skip=False)
    if not batches:
        add("receive", "receive", "收第一批图纸",
            "点「收图」，选收到的压缩包、文件或整个文件夹。会自动解开压缩包、去重、检查风险文件，"
            "原件只读保存在项目文件夹里，不会上传。", "receive", skip=False)
    ext = [i for i in issues if i["kind"] == "解包问题" and i["level"] == "warn"]
    if ext:
        add("receive", "extract", f"有 {len(ext)} 个压缩包没解开",
            "要密码、损坏或分卷不全的压缩包里可能藏着图纸。要到密码后重新收图（填在“压缩包密码”里）；"
            "损坏、分卷不全的请发图方重发。这里没什么可选的，看过点“知道了”。", "issue", ext, ack=True)
    fmt = [i for i in issues if i["id"] == "numbering"]
    if fmt and (st.get("numbering") or {}).get("status") == "proposed":
        add("setup", "numbering", "核对 AI 助手认的图号格式",
            "AI 助手认好了一份图号格式，在下面的“图号格式”卡片里。看一下每段的意思和码表，打“？”的是它也拿不准的。"
            "没问题就确认；不对就驳回，或者在 AI 助手里让它改。", "numbering", fmt)
    elif fmt:
        add("setup", "numbering", "确认图号格式",
            "图号每一段代表什么（楼栋、专业、楼层……），确认以后才能把图分对楼栋、专业和楼层，带版次后缀的文件也会并成一张图。"
            "看不懂没关系：让 AI 助手先认一遍，你只核对它打“？”的地方。没确认之前不影响缺图、版本的判断。", "numbering", fmt)
    tw = [i for i in issues if i["kind"] in ("塔楼不明", "塔楼叫法")]
    if tw:
        add("setup", "tower", f"确认 {len(tw)} 个楼栋问题",
            "有的文件看不出是哪栋楼的，或者同一栋楼有两种叫法。定下来以后，版本和缺图才能按栋算。"
            "每个问题下面有“怎么选？”。", "issue", tw)
    fb = [i for i in issues if i["kind"] == "楼层数法"]
    if fb:
        add("setup", "floor_base", "确认英文楼层怎么数" if len(fb) == 1 else f"确认 {len(fb)} 栋楼的英文楼层怎么数",
            "图名里有 Level 01 这样的写法：Level 01 是首层还是二层，各国习惯不一样。"
            "定下来以后，同层图纸和楼层覆盖才准；不影响缺图、版本。", "issue", fb)
    cad = st.get("cad_read") or {}
    if cad.get("unread") and cad.get("tool"):
        add("recognize", "read_dwg", f"读 DWG：还有 {cad['unread']} 个",
            "这些 DWG、DXF 是以前收的，还没读图签和文字。读的时候只在本机，图纸不出电脑；读出图号的 DWG 和看过图的 PDF 一样参与缺图、版本判断。",
            "read_dwg", [f"dwg:{cad['unread']}"])
    rec = st.get("recognition") or {}
    left = (rec.get("pages") or 0) - (rec.get("recognized") or 0)
    if left > 0:
        done = f"已识别 {rec['recognized']} 页。" if rec.get("recognized") else ""
        add("recognize", "recognize", f"看图识别：还有 {left} 页",
            done + "没识别的页是按文件名暂定的（图谱里的虚线框），不拿来判断缺图和版本；识别以后才能查缺图、比版本、对数字。"
            "先在「识别」里估算要多久，可以只识别一部分（先目录、说明、平面）。识别会把图纸页面发到外部，涉密的先确认能不能发。",
            "recognize", [f"left:{left}"])
    props = [p for p in st.get("proposals") or [] if p["kind"] != "numbering"]
    if props:
        add("issues", "proposals", f"核对 AI 助手的 {len(props)} 条提议",
            "AI 助手只能提议，不能直接改。每条看一下理由：同意就确认，不同意就驳回。", "proposals",
            [f"proposal:{p['id']}" for p in props])
    crit = [i for i in issues if i["level"] == "crit" and _open(i) and i["kind"] != "风险文件"]
    if crit:
        add("issues", "crit", f"处理 {len(crit)} 个严重问题",
            f"{_kinds(crit)}。每个问题下面有“怎么选？”。拿不准的先列入答疑；版本冲突、事实冲突不选也会写进答疑清单。",
            "issue", crit)
    rest = [i for i in issues if i["level"] == "warn" and _open(i) and i["kind"] not in SETUP_KINDS | {"风险文件", "解包问题"}]
    warn, notes = [i for i in rest if i["options"]], [i for i in rest if not i["options"]]
    if warn:
        add("issues", "warn", f"处理 {len(warn)} 个提醒",
            f"{_kinds(warn)}。提醒不急，建模前最好处理完；每个下面有“怎么选？”。", "issue", warn)
    if notes:
        add("issues", "notes", f"看一下 {len(notes)} 条提醒",
            f"{_kinds(notes)}。这几条没什么可选的，看过点“知道了”；有新的会再提醒。", "issue", notes, ack=True)
    if batches:
        add("export", "export", "导出现行图纸和答疑清单",
            "「导出」里可以导出只含现行版的图纸文件夹，建模只从这里取图，就碰不到旧版。答疑清单、缺图清单、台账在报告文件夹里，"
            "每次收图、识别后自动更新。开始建模前，可以在下面“建模任务的图纸基线”里锁定一份，图纸升版时会提醒。",
            "export", skip=False)
    stages, before = [], batches  # 前面的步骤都做完了，这一步才打勾：没识别完，“处理问题”就不算完
    for k, label in STAGES:
        before = before and k != "export" and not any(t["stage"] == k for t in tasks)
        stages.append({"key": k, "label": label, "done": before})
    return {"stages": stages, "tasks": tasks}


# ---------------- 做过的决定 ----------------

DECISION_GROUP = {"missing": "缺图", "sheet_current": "现行版", "sheet_pick": "现行版", "rev_check": "改图未升版",
                  "sheet_void": "作废还是漏发", "fact": "事实", "catalog_rev": "目录版次", "name_check": "文件名不符",
                  "file_tower": "楼栋", "tower_alias": "楼栋", "floor_base": "楼层数法", "numbering": "图号格式",
                  "risk": "风险文件"}


def decision_text(kind: str, key: str, value: str, names: dict | None = None) -> str:
    """把数据库里的决定翻译成人话。names：文件编号、页面编号 → 文件名（有就写文件名）。"""
    names = names or {}
    if kind == "sheet_void":
        return f"{key} {'确认已作废' if value == 'voided' else '仍然有效（漏发）'}"
    if kind == "sheet_current":
        return f"{key} 以 {value} 版为现行"
    if kind == "sheet_pick":
        no, _, rev = key.partition("|")
        return f"{no} {rev} 版采用" + (f" {names[value]}" if value in names else f"页面 {value}")
    if kind == "missing":
        what = key.split("|")
        what = f"{what[1]}缺 {what[2]}" if what[0] == "楼层" and len(what) == 3 else f"缺图 {key}"
        return f"{what}：{'列入答疑，向招标方要图' if value == 'ask' else '确认不需要'}"
    if kind == "fact":
        return f"事实「{key}」采用 {value}"
    if kind == "risk":
        who = names.get(key) or f"[{key}]"
        return f"风险文件 {who} {'确认安全（误报）' if value == 'safe' else '确认有风险，列入答疑'}"
    if kind == "catalog_rev":
        parts = key.split("|")
        what = f"{parts[0]} 目录写 {parts[1]} 版" if len(parts) == 2 else f"{parts[0]} 目录版次没跟上图纸"
        return f"{what}：{'列入答疑' if value == 'ask' else '以收到的版本为准'}"
    if kind == "name_check":
        no, _, fname = key.partition("|")
        return f"{no} 文件名和图签对不上（{fname}）：{'列入答疑' if value == 'ask' else '以图签为准'}"
    if kind == "rev_check":
        no, _, rev = key.partition("|")
        return f"{no} {rev} 版改了图没升版：{'列入答疑' if value == 'ask' else '以日期晚的为准'}"
    if kind == "file_tower":
        who = names.get(key) or f"文件 [{key}]"
        return f"{who} 是" + ("几栋共用的" if value == towers.COMMON else f" {value} 的")
    if kind == "tower_alias":
        return f"{key} 和 {value} 是同一栋楼" if value else f"{key} 不是别的楼栋的另一个叫法，单独算"
    if kind == "floor_base":
        label = floors.BASE_LABEL.get(int(value)) if value in ("0", "1") else value
        return f"{'全项目' if key == '*' else key} 英文楼层怎么数：{label}"
    if kind == "numbering":
        if value == "off":
            return "图号格式：先不用，按 ISO 英国附录的顺序读"
        try:
            return "图号格式：" + numbering.describe(json.loads(value))
        except (ValueError, TypeError, KeyError, AttributeError):
            return "图号格式：已确认"
    return f"{kind} {key} = {value}"


def decided(decisions: dict, names: dict | None = None) -> list[dict]:
    """现在起作用的人工决定（每项最后一次、没撤回的），新的在前；界面上能撤回。"""
    rows = sorted(decisions.values(), key=lambda r: r["id"], reverse=True)
    return [{"kind": r["kind"], "key": r["key"], "group": DECISION_GROUP.get(r["kind"], r["kind"]),
             "text": decision_text(r["kind"], r["key"], r["value"], names), "at": r["created_at"],
             "actor": r["actor"], "note": r["note"]} for r in rows]
