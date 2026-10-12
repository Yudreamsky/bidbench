"""日志：项目操作日志（命令行、工作台、数据库里的决定和提议合成一条时间线）、排错日志（不记密码）。"""

import json
import logging
import subprocess
import threading
import time
from datetime import date

import pytest

from bidbench import appconfig, cli, debuglog, extractors, oplog, server
from bidbench.assistant import Toolkit
from bidbench.store import Project, add_decision


def _entries(proj):
    rows = []
    for p in sorted((proj.root / "logs").glob("操作日志-*.jsonl")):
        rows += [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
    return rows


def _debug_text():
    return "".join(p.read_text(encoding="utf-8") for p in sorted((appconfig.config_dir() / "logs").glob("bidbench-*.log")))


def test_cli_commands_are_logged(sample_zips, tmp_path, capsys):
    root = tmp_path / "p"
    cli.main(["init", str(root), "--name", "甲"])
    (zip1, d1), (zip2, d2) = sample_zips[:2]
    cli.main(["-p", str(root), "receive", str(zip1), "--date", d1, "--no-av", "--password", "绝密口令"])
    with pytest.raises(SystemExit, match="找不到"):
        cli.main(["-p", str(root), "receive", str(tmp_path / "无.zip"), "--date", d2, "--no-av"])
    cli.main(["-p", str(root), "recognize"])
    cli.main(["-p", str(root), "decide", "sheet_void", "建施-05", "voided"])
    proj = Project.open(root)
    rows = _entries(proj)
    assert [e["title"] for e in rows] == ["新建项目「甲」", f"收图：{zip1.name}", "收图：无.zip", "模拟识别"]
    assert {e["source"] for e in rows} == {"命令行"}
    ok, bad = rows[1], rows[2]
    assert ok["status"] == "done" and ok["summary"].startswith("批次 #1：") and any("批次 #1" in ln for ln in ok["lines"])
    assert bad["status"] == "error" and "找不到" in bad["error"] and bad["summary"] is None
    assert rows[3]["kind"] == "recognize" and "导入" in rows[3]["summary"]

    capsys.readouterr()
    cli.main(["-p", str(root), "log", "--lines"])
    out = capsys.readouterr().out
    assert out.index("新建项目「甲」") < out.index("模拟识别") < out.index("决定：建施-05 确认已作废")  # 最新的在最下面
    assert "收图：无.zip · 命令行 —— 出错" in out and "排错日志" in out

    debug = _debug_text()
    assert "命令：bidbench -p" in debug and "--password ***" in debug and "绝密口令" not in debug
    assert "命令没有完成：找不到" in debug


def test_timeline_merges_decisions_and_proposals(demo_copy):
    add_decision(demo_copy, "missing", "楼层|地上楼层|5F", "ask", "已发答疑")
    ai = Toolkit(demo_copy)  # AI 助手只能提议
    issue = next(i for i in ai.list_issues()["issues"] if i["kind"] == "作废还是漏发")
    pid = ai.propose_issue_resolution(issue["id"], "1", "目录里已经没有这张图")["proposal_id"]
    Toolkit(demo_copy, source="工作台").confirm(pid)
    pid2 = ai.propose_note("层数", "机房层不计入层数", "答疑回复第 3 条")["proposal_id"]
    Toolkit(demo_copy, source="工作台").reject(pid2)
    oplog.write(demo_copy, "build", "重新计算报告", source="工作台", result={"as_of": 4, "crit": 1, "warn": 2})
    with open(demo_copy.root / "logs" / f"操作日志-{date.today():%Y-%m}.jsonl", "a", encoding="utf-8") as f:
        f.write('{"time": "2026-10-12T1\n')  # 写到一半断电留下的半行
    rows = oplog.read(demo_copy)
    titles = [e["title"] for e in rows]
    build = next(e for e in rows if e["title"] == "重新计算报告")
    assert build["summary"] == "截至批次 #4：严重问题 1 项，提醒 2 项" and build["source"] == "工作台"
    assert f"驳回提议 #{pid2}" in titles and f"确认提议 #{pid}" in titles
    assert any(t.startswith(f"提议 #{pid}：") for t in titles)
    assert "决定：地上楼层缺 5F：列入答疑，向招标方要图" in titles
    assert not any(e.get("actor") == "AI 助手建议，我确认" for e in rows)  # 确认提议那条已经写了结果，不重复
    assert "锁定基线「3层结构模型」" in titles
    proposal = next(e for e in rows if e["title"].startswith(f"提议 #{pid}："))
    assert proposal["source"] == "AI 助手" and proposal["summary"] == "理由：目录里已经没有这张图"
    times = [oplog.when(e["time"]) for e in rows]
    assert times == sorted(times, reverse=True)


def test_summary_tolerates_partial_results():
    assert oplog.summary("build", {}) is None
    assert oplog.summary("receive", None) is None
    assert oplog.summary("scan", {"pending": 0}) == "没有待确认的风险文件"


def test_write_failure_does_not_break_the_operation(demo_copy):
    (demo_copy.root / "logs").mkdir(exist_ok=True)
    bad = demo_copy.root / "logs" / f"操作日志-{date.today():%Y-%m}.jsonl"
    bad.mkdir()  # 文件位置被一个文件夹占了，写不进去
    e = oplog.write(demo_copy, "build", "重新计算报告", source="命令行")
    assert e["title"] == "重新计算报告"


@pytest.fixture
def workbench(demo_copy):
    debuglog.setup("test")
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    from test_desktop import Client
    yield Client(httpd)
    httpd.shutdown()


def test_workbench_jobs_and_decisions_are_logged(workbench, demo_copy, monkeypatch):
    c = workbench
    assert c.job("build")["status"] == "done"
    c.post("/api/decision", {"kind": "sheet_void", "key": "建施-05", "value": "keep"})

    def broken(proj, as_of=None, log=print):
        log("算到一半")
        raise KeyError("意外")
    monkeypatch.setattr(server.ops, "build", broken)
    j = c.job("build")
    assert j["status"] == "error" and "排错日志" in j["error"]

    data = c.get("/api/log")
    assert data["folder"].endswith("logs") and data["debug_folder"] == str(debuglog.folder())
    rows = data["entries"]
    assert "决定：建施-05 仍然有效（漏发）" in [e["title"] for e in rows]  # 和任务在同一秒时先后不定
    failed, done = [e for e in rows if e["title"] == "重新计算报告"]  # 最新的在前
    assert failed["status"] == "error" and failed["lines"][0] == "算到一半" and failed["source"] == "工作台"
    assert done["status"] == "done" and done["summary"].startswith("截至批次 #4")
    debug = _debug_text()
    assert "任务意外出错：重新计算报告" in debug and "Traceback" in debug and "KeyError: '意外'" in debug
    assert not (appconfig.config_dir() / "error.log").exists()  # 以前的 error.log 并进了排错日志


def test_window_closed_mid_job_is_logged_once(workbench, demo_copy, monkeypatch):
    c = workbench
    gate = threading.Event()
    monkeypatch.setattr(server.ops, "build", lambda proj, as_of=None, log=print: gate.wait(5) and {})
    c.post("/api/job", {"kind": "build"})
    c.httpd.app.abandon_job()  # 用户确认退出，窗口关了
    gate.set()
    for _ in range(200):
        if c.get("/api/job")["status"] != "running":
            break
        time.sleep(0.02)
    time.sleep(0.1)
    rows = [e for e in _entries(demo_copy) if e["title"] == "重新计算报告"]
    assert [e["status"] for e in rows] == ["interrupted"]


def test_new_project_from_workbench_starts_the_log(tmp_path):
    httpd = server.serve(None, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    from test_desktop import Client
    try:
        Client(httpd).post("/api/app/new", {"path": str(tmp_path / "乙"), "name": "乙"})
    finally:
        httpd.shutdown()
    assert [(e["title"], e["source"]) for e in _entries(Project.open(tmp_path / "乙"))] == [("新建项目「乙」", "工作台")]


def test_debug_log_hides_passwords_and_keeps_two_weeks(monkeypatch):
    d = debuglog.folder()
    (d / "bidbench-2020-01-01.log").write_text("旧的", encoding="utf-8")
    (d / f"bidbench-{date.today():%Y-%m-%d}.log").write_text("", encoding="utf-8")
    debuglog.setup("receive")
    debuglog.setup("receive")  # 重复调用不重复写
    assert not (d / "bidbench-2020-01-01.log").exists()
    assert sum(1 for h in logging.getLogger("bidbench").handlers if getattr(h, debuglog._TAG, False)) == 1
    assert debuglog.safe_command(["7z", "x", "-p-", "-pSECRET", "--password=x1", "D:/a b.7z"]) == \
        '7z x -p- -p*** --password=*** "D:/a b.7z"'
    monkeypatch.setattr(extractors.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", "Wrong password"))
    extractors._run(["7z", "x", "-y", "-p绝密", "a.7z"])
    text = _debug_text()
    assert "7z x -y -p*** a.7z → 返回 2" in text and "Wrong password" in text and "绝密" not in text
    assert text.count("启动：bidbench") == 2


def test_mcp_tool_calls_go_to_debug_log_without_text(demo_copy):
    pytest.importorskip("mcp")
    import asyncio

    from bidbench.mcp_server import build_server
    debuglog.setup("mcp")
    server = build_server(Toolkit(demo_copy))

    async def go():
        await server.call_tool("search", {"text": "机房层不计入"})
        await server.call_tool("get_sheet", {"no": "建施-99"})
    asyncio.run(go())
    text = _debug_text()
    assert "AI 助手调用 search（text=（6 字）" in text and "机房层" not in text
    assert "AI 助手调用 get_sheet（no=建施-99）：没有图号" in text
