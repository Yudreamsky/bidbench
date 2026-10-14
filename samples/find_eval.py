"""建模找图评测：给 make_model_sample.py 里的 5 个建模任务自动打分。

做法：用合成图纸建一个演练项目（模拟执行器识别，不调用真的 AI），然后对每个任务，
让“找图方法”只通过 AI 助手的工具（assistant.Toolkit，和 MCP 是同一套）去找图，
再和标准答案比：

- 该看的图（need）、相关的图（related）：找到的图里有没有；
- 缺的图（missing）：有没有指出这张图没发来；
- 变更单（notices）：有没有指出改过这些图的变更单。

分数 = 四样合起来找到了几成（召回率）。另外记“多给的图”（不在答案里的）和调用了几次工具，
分数一样时，多给得少、调用得少的更好。cautions 是给人看的提醒，不打分。

找图方法放在 FINDERS 里，签名是 finder(tk, task) -> {"sheets": [...], "missing": [...], "notices": [...]}。
现在只有 card：打开起点图的卡片（get_sheet），把卡片上列出的引用、同层图、通用图、变更单都算上。
以后加了“按任务找图”之类的新工具，在这里加一个找图方法，就能和 card 比分数。

用法：
  python samples/find_eval.py                    # 在临时目录建演练项目并打分
  python samples/find_eval.py --project <目录>    # 给已有的演练项目打分（例如已用 Codex 识别过的）
  python samples/find_eval.py --finder card --json 结果.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import make_model_sample  # noqa: E402

from bidbench import ingest, recognition  # noqa: E402
from bidbench.assistant import Toolkit, ToolError  # noqa: E402
from bidbench.executors import simulated  # noqa: E402
from bidbench.store import Project  # noqa: E402

KINDS = [("need", "该看的图"), ("related", "相关的图"), ("missing", "缺的图"), ("notices", "变更单")]


# ---------------- 演练项目 ----------------

def build_project(root: Path) -> Project:
    """逐批收图，每批收完用模拟执行器识别。"""
    zips = make_model_sample.build(root / "图纸")
    proj = Project.init(root / "项目", make_model_sample.PROJECT)
    for path, date in zips:
        ingest.receive(proj, path, date)
        task = recognition.make_task(proj, render=False)
        if task:
            simulated.run(task)
            res = recognition.import_results(proj, task / "results")
            if res.errors:
                raise RuntimeError(f"导入识别结果出错：{res.errors}")
    return proj


# ---------------- 找图方法 ----------------

class CountingToolkit:
    """包一层 Toolkit，记下调用了哪些工具、几次。"""

    def __init__(self, tk: Toolkit):
        self._tk = tk
        self.calls: list[str] = []

    def __getattr__(self, name):
        fn = getattr(self._tk, name)
        if not callable(fn) or name.startswith("_"):
            return fn

        def wrapped(*a, **kw):
            self.calls.append(name)
            return fn(*a, **kw)
        return wrapped


def find_by_card(tk, task: dict) -> dict:
    """打开起点图的卡片，卡片上列出的都算找到；被引用、状态是“缺失”的算缺图。"""
    card = tk.get_sheet(task["start_sheet"])
    sheets = [card["no"]]
    sheets += [r["sheet"] for r in card["references"]]
    sheets += [s["no"] for s in card["same_floor"] + card["general_sheets"]]
    statuses = {s["no"]: s["status"] for s in tk.list_sheets(limit=1000)["sheets"]}
    missing = [no for no in dict.fromkeys(sheets) if statuses.get(no) == "缺失"]
    return {"sheets": [no for no in dict.fromkeys(sheets) if no not in missing], "missing": missing,
            "notices": [n["id"] for n in card["notices"]]}


FINDERS = {"card": find_by_card}


# ---------------- 打分 ----------------

def score_task(task: dict, found: dict) -> dict:
    got = {"need": set(found.get("sheets", [])), "related": set(found.get("sheets", [])),
           "missing": set(found.get("missing", [])), "notices": set(found.get("notices", []))}
    rows = {}
    for kind, _label in KINDS:
        want = task[kind]
        hit = [x for x in want if x in got[kind]]
        rows[kind] = {"want": want, "hit": hit, "miss": [x for x in want if x not in hit]}
    answer = set(task["need"]) | set(task["related"]) | set(task["missing"])
    extra = sorted((set(found.get("sheets", [])) | set(found.get("missing", []))) - answer)
    return {"id": task["id"], "task": task["task"], "start_sheet": task["start_sheet"], "kinds": rows, "extra": extra}


def evaluate(proj: Project, finder: str = "card", tasks: list[dict] | None = None) -> dict:
    fn = FINDERS[finder]
    out = []
    for task in tasks or make_model_sample.TASKS:
        tk = CountingToolkit(Toolkit(proj))
        try:
            found = fn(tk, task)
            error = None
        except ToolError as e:
            found, error = {}, str(e)
        r = score_task(task, found)
        r["tool_calls"] = len(tk.calls)
        if error:
            r["error"] = error
        out.append(r)
    totals = {}
    for kind, _label in KINDS:
        want = sum(len(r["kinds"][kind]["want"]) for r in out)
        hit = sum(len(r["kinds"][kind]["hit"]) for r in out)
        totals[kind] = {"want": want, "hit": hit}
    want = sum(t["want"] for t in totals.values())
    hit = sum(t["hit"] for t in totals.values())
    return {"finder": finder, "score": round(100 * hit / want) if want else 100, "hit": hit, "want": want,
            "totals": totals, "extra": sum(len(r["extra"]) for r in out),
            "tool_calls": sum(r["tool_calls"] for r in out), "tasks": out}


def report(res: dict) -> str:
    lines = [f"建模找图评测（找图方法：{res['finder']}）",
             f"总分 {res['score']} 分：答案里的 {res['want']} 项找到了 {res['hit']} 项；"
             f"多给的图 {res['extra']} 张；调用工具 {res['tool_calls']} 次。", ""]
    lines.append("分项：" + "；".join(f"{label} {res['totals'][k]['hit']}/{res['totals'][k]['want']}"
                                     for k, label in KINDS))
    for r in res["tasks"]:
        lines += ["", f"{r['id']} {r['task']}（从 {r['start_sheet']} 开始）"]
        if r.get("error"):
            lines.append(f"  出错：{r['error']}")
        for kind, label in KINDS:
            k = r["kinds"][kind]
            if not k["want"]:
                continue
            s = f"  {label} {len(k['hit'])}/{len(k['want'])}"
            if k["miss"]:
                s += f"，没找到：{'、'.join(k['miss'])}"
            lines.append(s)
        if r["extra"]:
            lines.append(f"  多给的图：{'、'.join(r['extra'])}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="建模找图评测：给 5 个建模任务的找图结果自动打分")
    ap.add_argument("--project", help="已有的演练项目目录；不给就在临时目录新建一个（模拟执行器识别）")
    ap.add_argument("--finder", default="card", choices=sorted(FINDERS), help="找图方法")
    ap.add_argument("--json", help="把详细结果另存成 JSON 文件")
    args = ap.parse_args(argv)
    tmp = None
    try:
        if args.project:
            proj = Project.open(args.project)
        else:
            tmp = Path(tempfile.mkdtemp(prefix="bidbench-find-eval-"))
            proj = build_project(tmp)
        res = evaluate(proj, args.finder)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    print(report(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


if __name__ == "__main__":
    main()
