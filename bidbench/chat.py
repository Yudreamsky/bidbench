"""工作台的 AI 助手：用本机的 Codex（ChatGPT 订阅额度）回答这个项目的问题。

每个问题调用一次 codex exec，给它挂上 bidbench 的 MCP 工具（和外部 agent 用的是同一套，见 mcp_server.py）：
查询全部只读；处理问题、确认事实等改动只能“提议”，在面板里由你确认或驳回；
觉得工作台本身该改进时可以提“改进建议”，你确认后记进改进建议清单，由开发者处理。

连续追问：不用 Codex 自己的会话记录（--ephemeral，图纸信息不留在 Codex 里），每次把这次对话前几轮的问答附在提示里。
安全：只读沙箱、不读你的 Codex 设置、工作目录是空的临时文件夹，提示里要求只用 bidbench 工具。
对话记录存在项目的 logs/AI助手对话.jsonl（关掉再打开还能接着看），每个问题也记进操作日志。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import appconfig, oplog
from .assistant import AI_SOURCE, _summary
from .debuglog import tail
from .executors import codex
from .mcp_server import INSTRUCTIONS
from .proc import NO_WINDOW
from .store import Project, now_iso

log = logging.getLogger(__name__)

HISTORY = "AI助手对话.jsonl"
CONTEXT_TURNS = 6         # 附在提示里的前几轮问答
CONTEXT_CHARS = 1500      # 每个回答最多附多少字
MAX_QUESTION = 4000
TURN_TIMEOUT = 900        # 一个问题最多等 15 分钟
DEFAULT_EFFORT = "medium"
SHOW_TURNS = 30           # 面板上最多显示多少轮

EXTRA_RULES = """你在“投标图纸工作台”的 AI 助手面板里回答用户，另外遵守：
- 只用 bidbench 工具查询和提议，不要运行任何命令，不要读写任何文件。
- 回答简洁：先给结论，再给出处（图号、版次，必要时 page_id）；拿不准的要说明。用 Markdown 的短段落和列表。
- 用户要你处理问题、确认事实、锁定基线、记笔记时，用对应的 propose_* 工具生成提议，并告诉用户在面板里确认。
- 回答用户的过程中，如果发现工作台本身的不足（缺少需要的工具、数据或识别结果明显不对、用起来不方便），
  可以用 propose_improvement 提一条改进建议：只写问题和建议，不要写项目名称、图号、图纸内容等项目信息。"""

TOOL_LABEL = {
    "project_overview": "项目概况", "list_batches": "收图批次", "batch_diff": "批次差异", "compare_batches": "批次对比",
    "list_sheets": "图纸清单", "get_sheet": "图纸详情", "get_page": "页面内容", "search": "搜索", "list_facts": "事实清单",
    "get_fact": "事实详情", "floor_coverage": "楼层覆盖", "list_issues": "问题清单", "draft_qa": "答疑草稿",
    "list_baselines": "建模基线", "estimate_recognition": "识别估算", "list_notes": "项目笔记", "list_proposals": "提议清单",
    "propose_issue_resolution": "提议处理问题", "propose_fact_confirmation": "提议确认事实", "propose_baseline": "提议锁定基线",
    "propose_note": "提议记笔记", "propose_improvement": "提改进建议",
}


class ChatError(Exception):
    """这个问题没法问（Codex 找不到等）。消息直接给用户看。"""


@dataclass
class Turn:
    id: str
    question: str
    model: str
    started: str
    status: str = "running"               # running / done / error / stopped
    steps: list = field(default_factory=list)  # [{"id", "kind": tool/note, "text", "state": running/ok/failed}]
    answer: str = ""
    error: str | None = None
    proposals: list = field(default_factory=list)  # 这一轮生成的提议编号
    seconds: float | None = None
    usage: dict = field(default_factory=dict)


HIDDEN_ARGS = {"reason", "problem", "suggestion"}  # 长段的理由和建议，进度里不显示


def step_text(tool: str, args: dict | None) -> str:
    """工具调用写成一句人话：图纸详情 建施-05、搜索 机房层。长段的理由、建议不显示，开关类参数不显示。"""
    vals = [str(v) for k, v in (args or {}).items()
            if k not in HIDDEN_ARGS and v not in (None, "", [], {}) and not isinstance(v, bool)]
    shown = "、".join(vals)
    return TOOL_LABEL.get(tool, tool) + (f" {shown[:40]}" if shown else "")


def mcp_command(proj: Project) -> list[str]:
    """Codex 用来启动 bidbench 工具服务的命令：安装版用同目录的 bidbench.exe，源码版用当前的 Python。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("bidbench.exe" if os.name == "nt" else "bidbench")
        return [str(exe), "-p", str(proj.root), "mcp"]
    return [sys.executable, "-m", "bidbench", "-p", str(proj.root), "mcp"]


def build_command(c: codex.Codex, proj: Project, workdir: Path, model: str, effort: str, out: Path) -> list[str]:
    server = mcp_command(proj)
    env = {"BIDBENCH_CONFIG_DIR": str(appconfig.config_dir()), "PYTHONUTF8": "1"}
    toml = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731 - JSON 的字符串和数组写法也是合法的 TOML
    return [*c.cmd, "exec", "--ignore-user-config", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
            "--json", "--color", "never", "-C", str(workdir), "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "-c", f"mcp_servers.bidbench.command={toml(server[0])}",
            "-c", f"mcp_servers.bidbench.args={toml(server[1:])}",
            "-c", "mcp_servers.bidbench.env={" + ", ".join(f"{k} = {toml(v)}" for k, v in env.items()) + "}",
            "-c", "mcp_servers.bidbench.startup_timeout_sec=60", "-c", "mcp_servers.bidbench.tool_timeout_sec=180",
            "-o", str(out), "-"]


def build_prompt(question: str, context: list[Turn]) -> str:
    parts = [INSTRUCTIONS, EXTRA_RULES]
    if context:
        lines = ["这次对话前面的问答（供参考，可能已经过时，需要时重新查询）："]
        for t in context:
            ans = t.answer if len(t.answer) <= CONTEXT_CHARS else t.answer[:CONTEXT_CHARS] + "……（后略）"
            lines += [f"用户：{t.question}", f"助手：{ans}"]
        parts.append("\n".join(lines))
    parts.append(f"用户的问题：{question}")
    return "\n\n".join(parts)


def _popen_runner(cmd: list[str], prompt: str, on_line, procs: list, workdir: Path, cancel: threading.Event) -> int:
    """运行 codex exec，一行一行地把 JSONL 事件交给 on_line。procs 和 cancel 用来让“停止”能结束它。"""
    with open(workdir / "stderr.txt", "wb") as err:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, **NO_WINDOW)
        procs.append(p)
        if cancel.is_set():  # 刚启动就被停止了
            p.kill()
        timer = threading.Timer(TURN_TIMEOUT, p.kill)
        timer.start()
        try:
            p.stdin.write(prompt.encode("utf-8"))
            p.stdin.close()
            for raw in p.stdout:
                on_line(raw.decode("utf-8", "replace"))
            return p.wait()
        finally:
            timer.cancel()


class Chat:
    """一个项目的 AI 助手对话。一次只回答一个问题；回答在后台线程里进行，界面轮询 state()。"""

    def __init__(self, proj: Project, runner=None, find=None):
        self.proj = proj
        self.runner, self.find = runner, find  # 测试里换成假的；None 时用真的 Codex
        self.lock = threading.Lock()
        self.turns: list[Turn] = []
        self.conversation = secrets.token_hex(4)
        self.procs: dict[str, list] = {}               # 每个问题自己的 Codex 进程
        self.cancel: dict[str, threading.Event] = {}
        self._load_last()

    # ---- 记录 ----
    def _history(self) -> Path:
        return self.proj.root / oplog.FOLDER / HISTORY

    def _load_last(self) -> None:
        """打开项目时接上最近一次对话。"""
        p = self._history()
        rows = []
        for ln in (p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []):
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("conversation") and r.get("question"):
                rows.append(r)
        if not rows:
            return
        self.conversation = rows[-1]["conversation"]
        names = Turn.__dataclass_fields__
        self.turns = [Turn(**{k: v for k, v in r.items() if k in names})
                      for r in rows if r["conversation"] == self.conversation][-SHOW_TURNS:]
        for t in self.turns:
            if t.status == "running":  # 上次回答到一半就关了
                t.status, t.error = "stopped", "上次没有答完（工作台关闭了）"

    def _save(self, turn: Turn, conversation: str, status: str) -> None:
        try:
            with open(self.proj.dir(oplog.FOLDER) / HISTORY, "a", encoding="utf-8") as f:
                f.write(json.dumps({"conversation": conversation, **asdict(turn), "status": status},
                                   ensure_ascii=False) + "\n")
        except OSError:
            log.exception("AI 助手对话记录写不进去")

    def _oplog(self, turn: Turn, status: str) -> None:
        q = turn.question.replace("\n", " ")
        tools = [s for s in turn.steps if s["kind"] == "tool"]
        lines = [f"查询：{s['text']}" for s in tools] + ([f"回答：{turn.answer}"] if turn.answer else [])
        oplog.write(self.proj, "assistant", f"问 AI 助手：{q[:40]}{'…' if len(q) > 40 else ''}", source="工作台",
                    status={"done": "done", "stopped": "interrupted"}.get(status, "error"), lines=lines,
                    error=turn.error, started=turn.started, seconds=turn.seconds,
                    result={"tools": len(tools), "proposals": len(turn.proposals)})

    # ---- 状态 ----
    @property
    def running(self) -> bool:
        return bool(self.turns) and self.turns[-1].status == "running"

    def state(self) -> dict:
        with self.lock:
            turns = [asdict(t) for t in self.turns[-SHOW_TURNS:]]
        # “还在回答”取自同一份快照：下面读提议的工夫里回答可能刚好完成，
        # 若另算，界面会拿到“不在回答、最后一轮却还在查”，停在“正在查”不再刷新
        running = bool(turns) and turns[-1]["status"] == "running"
        ids = sorted({i for t in turns for i in t["proposals"]})
        info = {}
        if ids:
            with self.proj.connect() as con:
                q = ",".join("?" * len(ids))
                for r in con.execute(f"SELECT * FROM proposals WHERE id IN ({q})", ids):
                    info[r["id"]] = {"id": r["id"], "kind": r["kind"], "status": r["status"], "reason": r["reason"],
                                     "summary": _summary(r["kind"], json.loads(r["payload"])), "outcome": r["outcome"]}
        for t in turns:
            t["proposals"] = [info[i] for i in t["proposals"] if i in info]
        return {"conversation": self.conversation, "running": running, "turns": turns}

    def _max_proposal(self) -> int:
        with self.proj.connect() as con:
            return con.execute("SELECT COALESCE(MAX(id), 0) FROM proposals").fetchone()[0]

    def _new_proposals(self, after: int) -> list[int]:
        with self.proj.connect() as con:
            return [r[0] for r in con.execute("SELECT id FROM proposals WHERE id>? AND source=? ORDER BY id",
                                              (after, AI_SOURCE))]

    # ---- 操作 ----
    def reset(self) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("还在回答上一个问题，先停止它再开新对话")
            self.turns, self.conversation = [], secrets.token_hex(4)

    def stop(self) -> None:
        """停止正在回答的问题：界面上马上显示“已停止”，后台结束 Codex 进程、写好记录。"""
        with self.lock:
            if not self.running:
                return
            turn = self.turns[-1]
            turn.status, turn.error = "stopped", "已停止"
            self.cancel.setdefault(turn.id, threading.Event()).set()
            procs = list(self.procs.get(turn.id, []))
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass

    def ask(self, question: str, model: str | None = None, effort: str | None = None) -> Turn:
        question = (question or "").strip()
        if not question:
            raise ValueError("请先输入问题")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"问题太长了，请精简到 {MAX_QUESTION} 字以内")
        with self.lock:
            if self.running:
                raise RuntimeError("还在回答上一个问题，等它答完或先停止")
            context = [t for t in self.turns if t.status == "done"][-CONTEXT_TURNS:]
            turn = Turn(id=secrets.token_hex(4), question=question, model=model or codex.DEFAULT_MODEL, started=now_iso())
            self.turns.append(turn)
            self.cancel[turn.id] = threading.Event()
        log.info("AI 助手提问：%s 字，模型 %s", len(question), turn.model)  # 排错日志不记问题原文
        threading.Thread(target=self._run, args=(turn, context, effort or DEFAULT_EFFORT, self.conversation),
                         daemon=True, name="chat").start()
        return turn

    def _event(self, turn: Turn, line: str, errors: list) -> None:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(e, dict):
            return
        it = e.get("item") or {}
        kind = it.get("type")
        with self.lock:
            if kind == "mcp_tool_call" and e.get("type") in ("item.started", "item.completed"):
                state = "running" if e["type"] == "item.started" else ("ok" if it.get("status") == "completed" else "failed")
                step = next((s for s in turn.steps if s.get("id") == it.get("id")), None)
                if step is None:
                    turn.steps.append({"id": it.get("id"), "kind": "tool", "text": step_text(it.get("tool", ""), it.get("arguments")),
                                       "state": state})
                else:
                    step["state"] = state
            elif kind == "agent_message" and e.get("type") == "item.completed" and (it.get("text") or "").strip():
                turn.steps.append({"id": it.get("id"), "kind": "note", "text": it["text"].strip(), "state": "ok"})
            elif e.get("type") == "turn.completed":
                for k, v in (e.get("usage") or {}).items():
                    if isinstance(v, int):
                        turn.usage[k] = turn.usage.get(k, 0) + v
            elif e.get("type") == "error":
                errors.append(codex._message(str(e.get("message", ""))))
            elif e.get("type") == "turn.failed":
                errors.append(codex._message(str((e.get("error") or {}).get("message", ""))))

    def _run(self, turn: Turn, context: list[Turn], effort: str, conversation: str) -> None:
        t0 = time.monotonic()
        before = self._max_proposal()
        errors: list[str] = []
        cancel = self.cancel[turn.id]
        procs = self.procs.setdefault(turn.id, [])
        status, answer, error = "error", "", None
        try:
            c = (self.find or codex.find_codex)()
            if c is None:
                raise ChatError(f"没有找到 Codex。安装：{codex.INSTALL_HINT}，再在命令行运行 codex login 登录")
            with tempfile.TemporaryDirectory(prefix="bidbench-chat-") as tmp:
                out = Path(tmp) / "answer.md"
                cmd = build_command(c, self.proj, Path(tmp), turn.model, effort, out)
                code = -1
                if not cancel.is_set():
                    code = (self.runner or _popen_runner)(cmd, build_prompt(turn.question, context),
                                                          lambda ln: self._event(turn, ln, errors), procs, Path(tmp), cancel)
                answer = out.read_text(encoding="utf-8").strip() if out.exists() else ""
                err_file = Path(tmp) / "stderr.txt"
                err_tail = tail(err_file.read_text(encoding="utf-8", errors="replace"), 300) if err_file.exists() else ""
            if cancel.is_set():
                status, error, answer = "stopped", "已停止", ""
            elif answer:
                status = "done"
            else:
                msg = errors[-1] if errors else (err_tail or f"Codex 没有给出回答（返回码 {code}）")
                error = codex.classify(msg, turn.model) or f"Codex 出错：{msg}"
        except ChatError as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001 - 意外错误告诉用户，并在排错日志里留下完整记录
            error = f"出错了：{type(e).__name__}: {e}。详细信息已写进排错日志"
            log.exception("AI 助手回答出错")
        finally:
            with self.lock:
                turn.answer, turn.error = answer, error
                if status == "done" and turn.steps and turn.steps[-1]["kind"] == "note" and turn.steps[-1]["text"] == answer:
                    turn.steps.pop()  # 最后一段就是回答本身
                turn.seconds = round(time.monotonic() - t0, 1)
            try:
                turn.proposals = self._new_proposals(before)
            except Exception:  # noqa: BLE001 - 读不到提议不影响回答
                log.exception("读取 AI 助手的新提议出错")
            log.info("AI 助手回答：%s，%.0f 秒，查询 %s 次，提议 %s 条，用量 %s", status, turn.seconds,
                     sum(1 for s in turn.steps if s["kind"] == "tool"), len(turn.proposals), turn.usage)
            self._save(turn, conversation, status)
            self._oplog(turn, status)
            with self.lock:
                turn.status = status  # 最后才标完成：界面看到完成时，对话记录和操作日志都已写好
                self.procs.pop(turn.id, None)
                self.cancel.pop(turn.id, None)
