"""工作台的 AI 助手（Codex 驱动）：用假的 Codex 模拟查询工具、生成提议、出错、被停止，不联网、不占额度。"""

import json
import threading
import time
from pathlib import Path

import pytest

from bidbench import chat, cli, oplog, ops, server
from bidbench.assistant import Toolkit, ToolError, list_suggestions
from bidbench.executors import codex

FAKE = codex.Codex(["codex"], "0.162.1")


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class FakeCodex:
    """假的 codex exec：按顺序报告工具调用和说明，可以顺手生成一条提议，最后写出回答。"""

    def __init__(self, proj, answer="还缺 **建施-10**、建施-11 两张图。", propose=False, fail=None, block=False):
        self.proj, self.answer, self.propose, self.fail, self.block = proj, answer, propose, fail, block
        self.calls = []
        self.started = threading.Event()

    def __call__(self, cmd, prompt, on_line, procs, workdir, cancel):
        self.calls.append({"cmd": cmd, "prompt": prompt})
        self.started.set()
        emit = lambda e: on_line(json.dumps(e, ensure_ascii=False) + "\n")  # noqa: E731
        emit({"type": "thread.started", "thread_id": "t"})
        if self.block:  # 等着被停止
            cancel.wait(5)
            return -9
        if self.fail:
            emit({"type": "error", "message": json.dumps({"error": {"message": self.fail}})})
            emit({"type": "turn.failed", "error": {"message": self.fail}})
            return 1
        emit({"type": "item.completed", "item": {"id": "m0", "type": "agent_message", "text": "我先查一下缺图。"}})
        emit({"type": "item.started", "item": {"id": "c1", "type": "mcp_tool_call", "server": "bidbench", "tool": "list_sheets",
                                               "arguments": {"status": "缺失"}, "status": "in_progress"}})
        emit({"type": "item.completed", "item": {"id": "c1", "type": "mcp_tool_call", "server": "bidbench", "tool": "list_sheets",
                                                 "arguments": {"status": "缺失"}, "status": "completed"}})
        if self.propose:  # 模拟助手调用 propose_note
            Toolkit(self.proj).propose_note("缺图", "建施-10、11 列入答疑", "两张图编号断号，各批都没收到")
            emit({"type": "item.completed", "item": {"id": "c2", "type": "mcp_tool_call", "server": "bidbench",
                                                     "tool": "propose_note", "arguments": {"topic": "缺图", "text": "…", "reason": "…"},
                                                     "status": "completed"}})
        emit({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": self.answer}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 100000, "cached_input_tokens": 80000, "output_tokens": 300}})
        Path(_arg(cmd, "-o")).write_text(self.answer, encoding="utf-8")
        return 0


def _wait(c, timeout=10):
    t0 = time.time()
    while c.state()["running"]:
        assert time.time() - t0 < timeout, "AI 助手没有答完"
        time.sleep(0.02)
    return c.state()


def test_answer_with_tools_proposals_and_followup(demo_copy):
    fake = FakeCodex(demo_copy, propose=True)
    c = chat.Chat(demo_copy, runner=fake, find=lambda: FAKE)
    c.ask("还缺哪些图？", model="gpt-6.1-sol")
    st = _wait(c)
    t = st["turns"][0]
    assert t["status"] == "done" and t["answer"].startswith("还缺 **建施-10**")
    assert [s["text"] for s in t["steps"]] == ["我先查一下缺图。", "图纸清单 缺失", "提议记笔记 缺图、…"]
    assert t["steps"][1]["state"] == "ok" and t["usage"]["input_tokens"] == 100000
    (p,) = t["proposals"]
    assert p["status"] == "pending" and p["summary"] == "项目笔记「缺图」：建施-10、11 列入答疑"

    cmd = fake.calls[0]["cmd"]
    for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json"):
        assert flag in cmd
    assert _arg(cmd, "--sandbox") == "read-only" and _arg(cmd, "-m") == "gpt-6.1-sol"
    server_args = json.loads(next(x for x in cmd if x.startswith("mcp_servers.bidbench.args=")).split("=", 1)[1])
    assert server_args[-3:] == ["-p", str(demo_copy.root), "mcp"]
    assert any(x.startswith("mcp_servers.bidbench.env={BIDBENCH_CONFIG_DIR = ") for x in cmd)
    prompt = fake.calls[0]["prompt"]
    assert "propose_improvement" in prompt and "不要运行任何命令" in prompt and prompt.endswith("用户的问题：还缺哪些图？")
    assert "前面的问答" not in prompt

    asked = next(e for e in oplog.read(demo_copy) if e["kind"] == "assistant")  # 和演示项目的记录可能在同一秒，按类型找
    assert asked["title"] == "问 AI 助手：还缺哪些图？" and asked["summary"] == "查询 2 次，生成提议 1 条"

    c.ask("那建施-11 呢？")  # 追问：带上前面的问答
    _wait(c)
    prompt2 = fake.calls[1]["prompt"]
    assert "用户：还缺哪些图？" in prompt2 and "助手：还缺 **建施-10**" in prompt2
    assert _arg(fake.calls[1]["cmd"], "-m") == codex.DEFAULT_MODEL

    again = chat.Chat(demo_copy, runner=fake, find=lambda: FAKE)  # 关掉工作台再打开：接上最近的对话
    assert [t["question"] for t in again.state()["turns"]] == ["还缺哪些图？", "那建施-11 呢？"]
    again.reset()
    assert again.state()["turns"] == [] and again.conversation != c.conversation
    again.ask("地上几层？")
    _wait(again)
    assert "前面的问答" not in fake.calls[2]["prompt"]
    history = (demo_copy.root / "logs" / chat.HISTORY).read_text(encoding="utf-8").splitlines()
    assert len(history) == 3 and json.loads(history[-1])["status"] == "done"


def test_stop_errors_and_guards(demo_copy):
    blocker = FakeCodex(demo_copy, block=True)
    c = chat.Chat(demo_copy, runner=blocker, find=lambda: FAKE)
    c.ask("慢慢想")
    assert blocker.started.wait(5)
    with pytest.raises(RuntimeError, match="还在回答"):
        c.ask("再问一个")
    with pytest.raises(RuntimeError, match="先停止"):
        c.reset()
    c.stop()
    assert c.state()["turns"][-1]["status"] == "stopped"  # 马上显示已停止
    time.sleep(0.2)
    assert json.loads((demo_copy.root / "logs" / chat.HISTORY).read_text(encoding="utf-8").splitlines()[-1])["status"] == "stopped"
    assert next(e for e in oplog.read(demo_copy) if e["kind"] == "assistant")["status"] == "interrupted"

    bad = FakeCodex(demo_copy, fail="The 'gpt-9' model is not supported when using Codex with a ChatGPT account.")
    c2 = chat.Chat(demo_copy, runner=bad, find=lambda: FAKE)
    c2.ask("你好", model="gpt-9")
    t = _wait(c2)["turns"][-1]
    assert t["status"] == "error" and "npm install -g @openai/codex@latest" in t["error"]

    c3 = chat.Chat(demo_copy, runner=bad, find=lambda: None)
    c3.ask("你好")
    assert "没有找到 Codex" in _wait(c3)["turns"][-1]["error"]
    with pytest.raises(ValueError, match="请先输入问题"):
        c3.ask("   ")


def test_state_running_matches_last_turn(demo_copy, monkeypatch):
    """“还在回答”和最后一轮的状态来自同一时刻：读提议的工夫里回答刚好完成，也不能出现
    “不在回答、最后一轮却还在查”（界面会停止刷新，一直显示“正在查”）。"""
    c = chat.Chat(demo_copy, runner=FakeCodex(demo_copy), find=lambda: FAKE)
    turn = chat.Turn(id="t1", question="还缺哪些图？", model="m", started="2026-10-13T00:00:00", proposals=[1])
    c.turns.append(turn)
    real = demo_copy.connect

    def connect_and_finish():  # 读提议时，后台线程正好把这一轮标成完成
        turn.status = "done"
        return real()
    monkeypatch.setattr(demo_copy, "connect", connect_and_finish)
    st = c.state()
    assert st["running"] is True and st["turns"][-1]["status"] == "running"
    assert c.state()["running"] is False  # 下一次就看到完成


def test_step_text_hides_free_text():
    assert chat.step_text("get_sheet", {"no": "建施-05"}) == "图纸详情 建施-05"
    assert chat.step_text("propose_issue_resolution", {"issue_id": "void:建施-05", "option": "1", "reason": "很长的理由"}) == \
        "提议处理问题 void:建施-05、1"  # 理由不显示
    assert chat.step_text("search", {"text": "机房层", "current_only": True}) == "搜索 机房层"  # 开关类参数不显示


def test_improvement_suggestions(demo_copy, capsys):
    ai = Toolkit(demo_copy)
    with pytest.raises(ToolError, match="问题"):
        ai.propose_improvement("缺工具", "", "加一个按楼层查图的工具")
    a = ai.propose_improvement("按楼层查图", "想知道 5 层有哪些图，只能一张张看", "加一个按楼层列出图纸的工具")
    b = ai.propose_improvement("答疑导出", "答疑清单只能复制", "能导出成 Word")
    me = Toolkit(demo_copy, source="工作台")
    assert me.confirm(a["proposal_id"])["outcome"] == "已记入改进建议清单「按楼层查图」，开发时处理"
    me.reject(b["proposal_id"])
    rows = list_suggestions()
    assert [r["title"] for r in rows] == ["按楼层查图"] and rows[0]["problem"].startswith("想知道 5 层")
    cli.main(["suggestions"])
    out = capsys.readouterr().out
    assert "1. 【按楼层查图】" in out and "建议：加一个按楼层列出图纸的工具" in out
    titles = [e["title"] for e in oplog.read(demo_copy)]
    assert any(t.startswith(f"提议 #{a['proposal_id']}：改进建议「按楼层查图」") for t in titles)


def test_mcp_lists_improvement_tool(demo_copy):
    pytest.importorskip("mcp")
    import asyncio
    from bidbench.mcp_server import build_server

    async def names():
        return {t.name: t for t in await build_server(Toolkit(demo_copy)).list_tools()}
    tools = asyncio.run(names())
    assert "propose_improvement" in tools and "confirm" not in tools


def test_workbench_chat_api(demo_copy, monkeypatch):
    from test_desktop import Client
    fake = FakeCodex(demo_copy, propose=True)
    monkeypatch.setattr(chat, "_popen_runner", fake)
    monkeypatch.setattr(codex, "find_codex", lambda refresh=False: FAKE)
    ready = {"found": True, "ready": True, "version": "0.162.1", "default_model": "gpt-6.1-sol", "hint": None,
             "models": [{"slug": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "description": "", "efforts": []}]}
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: ready)
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        assert c.post("/api/chat", {"question": "还缺哪些图？"}, token=False)["_status"] == 403
        assert c.post("/api/chat", {"question": "x", "model": "--config=evil"})["_status"] == 400
        r = c.post("/api/chat", {"question": "还缺哪些图？", "model": "gpt-6.1-sol"})
        assert r["running"] and r["turns"][-1]["question"] == "还缺哪些图？"
        for _ in range(500):
            st = c.get("/api/chat")
            if not st["running"]:
                break
            time.sleep(0.02)
        assert st["turns"][-1]["status"] == "done" and st["turns"][-1]["proposals"][0]["status"] == "pending"
        pid = st["turns"][-1]["proposals"][0]["id"]
        assert pid in [p["id"] for p in c.get("/api/state")["proposals"]]  # 也出现在问题中心
        c.post("/api/proposal", {"id": pid, "action": "confirm"})
        assert c.get("/api/chat")["turns"][-1]["proposals"][0]["status"] == "confirmed"
        full = c.get("/api/chat?status=1")
        assert full["codex"]["ready"] and full["prefs"] == {"model": "gpt-6.1-sol"} and full["suggestions"] == 0
        assert c.post("/api/chat/new")["turns"] == []
        ready["ready"], ready["hint"] = False, "Codex 还没有登录：在命令行运行 codex login，用 ChatGPT 账号登录。"
        assert "codex login" in c.post("/api/chat", {"question": "你好"})["error"]
    finally:
        httpd.shutdown()
