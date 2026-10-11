"""命令行入口：bidbench <命令>。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import export, ingest, recognition, reports, state
from .executors import simulated
from .store import Project, add_decision


def _proj(args) -> Project:
    return Project.open(Path(args.project))


def cmd_init(args):
    p = Project.init(Path(args.dir), args.name)
    print(f"已创建项目「{args.name}」：{p.root}")


def cmd_receive(args):
    proj = _proj(args)
    r = ingest.receive(proj, Path(args.archive), args.date, args.name, args.password, args.note)
    rep = reports.receive_report(proj, r)
    kinds = "、".join(f"{reports.KIND_LABEL.get(k, k)} {n}" for k, n in sorted(r.by_kind.items()))
    print(f"批次 #{r.seq}「{r.name}」：解出 {r.files} 个文件（{kinds}），新增 {r.new_pages} 页。")
    if r.nested_archives:
        print(f"  内层压缩包 {len(r.nested_archives)} 个，已自动展开。")
    for i in r.issues:
        print(f"  ⚠ {reports.ISSUE_LABEL.get(i['kind'], i['kind'])}：{i['path']}")
    print(f"解包报告：{rep}")


def cmd_tasks(args):
    proj = _proj(args)
    t = recognition.make_task(proj, include_done=args.all, render=not args.no_images)
    if not t:
        print("没有需要识别的页面。")
        return
    n = len(json.loads((t / "manifest_in.json").read_text(encoding="utf-8"))["pages"])
    print(f"已生成识别任务包（{n} 页）：{t}")
    print("交给外部 agent 按 任务说明.md 执行，或运行 bidbench recognize 用内置模拟执行器。")


def cmd_recognize(args):
    proj = _proj(args)
    if args.executor != "simulated":
        sys.exit(f"暂不支持执行器 {args.executor}；外部 agent 请用 bidbench tasks + bidbench import。")
    task = Path(args.task) if args.task else None
    if task is None:
        task = recognition.make_task(proj, render=False)
        if task is None:
            print("没有需要识别的页面。")
            return
    n = simulated.run(task)
    res = recognition.import_results(proj, task / "results")
    print(f"模拟执行器识别 {n} 页，导入 {res.imported} 页。" + (f"{len(res.errors)} 个文件未通过校验。" if res.errors else ""))


def cmd_import(args):
    proj = _proj(args)
    res = recognition.import_results(proj, Path(args.results))
    print(f"导入 {res.imported} 页，{res.unchanged} 页无变化。")
    if res.errors:
        print(f"{len(res.errors)} 个文件未通过校验，详见 reports/识别结果导入问题.md")
        for e in res.errors[:5]:
            print(f"  {e['file']}：{e['error']}")


def cmd_build(args):
    proj = _proj(args)
    st = state.build(proj, args.as_of)
    is_latest = st["as_of"] == max((b["seq"] for b in st["batches"]), default=0)
    sub = None if is_latest else f"截至批次{st['as_of']:02d}"
    out = proj.path("exports", "state.json") if is_latest else proj.path("reports", sub, "state.json")
    out.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    files = reports.write_all(proj, st, sub)
    crit = sum(1 for i in st["issues"] if i["level"] == "crit")
    warn = sum(1 for i in st["issues"] if i["level"] == "warn")
    print(f"截至批次 #{st['as_of']}：{len(st['sheets'])} 个图号，严重问题 {crit} 项，提醒 {warn} 项。")
    for i in st["issues"]:
        if i["level"] != "info":
            print(f"  {'●' if i['level'] == 'crit' else '○'} [{i['kind']}] {i['title']}")
    print(("历史状态报告（reports/" + sub + "）：" if sub else "报告：") + "、".join(p.name for p in files))


def cmd_decide(args):
    proj = _proj(args)
    add_decision(proj, args.kind, args.key, args.value, args.note)
    print(f"已记录决定：{args.kind} {args.key} = {args.value}")


def cmd_baseline(args):
    proj = _proj(args)
    snap = state.create_baseline(proj, args.name, args.sheets.split(",") if args.sheets else None, args.note)
    print(f"已锁定基线「{args.name}」：{len(snap)} 张图。")


def cmd_export(args):
    proj = _proj(args)
    st = state.build(proj, with_diff=False)
    if args.what == "current":
        print(f"现行图纸已导出：{export.export_current(proj, st, Path(args.out) if args.out else None)}")
    else:
        print(f"数据镜像已导出：{export.export_mirror(proj, st, Path(args.out) if args.out else None)}")


def cmd_serve(args):
    from .server import serve
    proj = _proj(args)
    httpd = serve(proj, args.host, args.port)
    print(f"工作台已启动：http://{args.host}:{args.port}/  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bidbench", description="投标图纸工作台")
    ap.add_argument("-p", "--project", default=".", help="项目目录（默认当前目录）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="新建项目")
    s.add_argument("dir")
    s.add_argument("--name", required=True)
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("receive", help="收图：登记一个压缩包为新批次并递归解包")
    s.add_argument("archive")
    s.add_argument("--date", required=True, help="收图日期 YYYY-MM-DD")
    s.add_argument("--name")
    s.add_argument("--password", action="append", default=[], help="压缩包密码，可多次指定")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_receive)

    s = sub.add_parser("tasks", help="生成识别任务包，交给外部 agent 执行")
    s.add_argument("--all", action="store_true", help="包括已识别过的页面")
    s.add_argument("--no-images", action="store_true", help="不渲染页面图片（仅文字层）")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("recognize", help="用内置执行器识别并导入")
    s.add_argument("--executor", default="simulated")
    s.add_argument("--task", help="已有任务包路径；不填则新建")
    s.set_defaults(fn=cmd_recognize)

    s = sub.add_parser("import", help="校验并导入识别结果")
    s.add_argument("results")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("build", help="计算台账、缺图、事实层并写报告")
    s.add_argument("--as-of", type=int, help="还原到第几批的状态")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("decide", help="记录人工决定")
    s.add_argument("kind", choices=["sheet_pick", "sheet_current", "sheet_void", "missing", "fact"])
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("baseline", help="为建模等任务锁定图纸基线")
    s.add_argument("name")
    s.add_argument("--sheets", help="逗号分隔的图号；不填为全部现行图纸")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("export", help="导出现行图纸文件夹或 agent 数据镜像")
    s.add_argument("what", choices=["current", "mirror"])
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("serve", help="启动本地工作台")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        sys.exit(f"错误：{e}")


if __name__ == "__main__":
    main()
