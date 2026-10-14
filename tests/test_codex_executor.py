"""Codex 执行器测试：用假的 Codex（只模拟命令行的输入输出），不联网、不占 ChatGPT 额度。"""

import functools
import json
import re
import subprocess
from pathlib import Path

import pytest

from bidbench import cli, oplog, ops, recognition, server
from bidbench.executors import claude, codex, simulated
from bidbench.recognition import load_schema


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _body_from_prompt(prompt: str) -> dict:
    """像模型一样：从提示里的文字层生成识别结果（借用规则执行器）。"""
    pid = re.search(r"页面编号 (\S+?)，", prompt).group(1)
    lines = []
    for ln in prompt.split("归一化位置）：\n", 1)[-1].splitlines():
        m = re.match(r"^\[([\d.,]+)\] (.*)$", ln)
        if m:
            lines.append({"text": m.group(2), "bbox": [float(x) for x in m.group(1).split(",")]})
    item = {"page_id": pid, "source": {"file_path": "x", "file_sha256": "0" * 64, "page_index": 1, "page_count": 1,
                                       "has_text_layer": bool(lines)}}
    rec = simulated.recognize_page(item, lines)
    return {k: rec[k] for k in claude.BODY_KEYS}


class FakeCodex:
    """假的 codex exec：记下命令和提示，按 answer(prompt, n) 写出 -o 文件、打印 JSONL 事件。"""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or (lambda prompt, n: (_body_from_prompt(prompt), None))

    def __call__(self, cmd, prompt, timeout):
        self.calls.append({"cmd": cmd, "prompt": prompt})
        body, error = self.answer(prompt, len(self.calls))
        events = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
        if error:
            events += [{"type": "error", "message": json.dumps({"type": "error", "error": {"message": error}})},
                       {"type": "turn.failed", "error": {"message": error}}]
            return 1, "\n".join(json.dumps(e) for e in events), "Reading prompt from stdin..."
        Path(_arg(cmd, "-o")).write_text(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False),
                                         encoding="utf-8")
        events.append({"type": "turn.completed", "usage": {"input_tokens": 26000, "cached_input_tokens": 12000,
                                                           "output_tokens": 900}})
        return 0, "\n".join(json.dumps(e) for e in events), "Reading prompt from stdin..."


def test_output_schema_is_openai_strict():
    s = codex.output_schema()

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if "$ref" in n:
                assert set(n) == {"$ref"}, n  # $ref 旁边不能有 description 等
            if isinstance(n.get("properties"), dict):
                assert n["required"] == list(n["properties"]) and n["additionalProperties"] is False
            for v in n.values():
                walk(v)
    walk(s)
    # 完整格式里可选的字段都必须允许 null，列为必填后才不改变含义
    full = load_schema()

    def optional_allow_null(n):
        if isinstance(n, list):
            for x in n:
                optional_allow_null(x)
        elif isinstance(n, dict):
            props = n.get("properties")
            if isinstance(props, dict):
                for k, v in props.items():
                    if k not in n.get("required", []):
                        target = full["$defs"][v["$ref"].split("/")[-1]] if "$ref" in v else v
                        allows = "null" in json.dumps(target.get("type", "")) or any(
                            b.get("type") == "null" for b in target.get("oneOf", []) + target.get("anyOf", []))
                        assert allows, k
            for v in n.values():
                optional_allow_null(v)
    optional_allow_null(full)


def test_end_to_end_with_fake_codex(demo_copy):
    task = recognition.make_task(demo_copy, scope="conflict")
    pages = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    fake = FakeCodex()
    lines = []
    r = codex.run(task, "economy", runner=fake, workers=2, log_fn=lines.append)
    assert r.done == len(pages) and not r.failed and r.model == codex.DEFAULT_MODEL
    cmd = fake.calls[0]["cmd"]
    for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json"):
        assert flag in cmd  # 不读用户的 Codex 设置、不留会话记录
    assert _arg(cmd, "--sandbox") == "read-only" and _arg(cmd, "-m") == "gpt-6.1-sol"
    assert 'model_reasoning_effort="medium"' in cmd and cmd[-1] == "-"
    images = [Path(_arg(cmd[i:], "-i")) for i, x in enumerate(cmd) if x == "-i"]
    assert len(images) == 2  # 整页 + 图签，经济档不切片
    assert Path(_arg(cmd, "-C")) == images[0].parent  # 工作目录是只放了这一页图片的临时文件夹
    assert not images[0].exists()  # 用完就删
    prompt = fake.calls[0]["prompt"]
    assert claude.SYSTEM_PROMPT in prompt and "第 2 张：图签区域放大图" in prompt and "不要运行任何命令" in prompt
    assert lines[0].startswith(f"开始识别 {len(pages)} 页：Codex，模型 gpt-6.1-sol，思考强度 medium")
    assert sum(1 for ln in lines if "✓" in ln) == len(pages) and "输入 26000 / 输出 900 tokens" in lines[1]
    log = (task / "results" / "run_log.md").read_text(encoding="utf-8")
    assert "Codex 额度，不另外计费" in log and "问题：无" in log
    usage = json.loads((task / "results" / "usage.json").read_text(encoding="utf-8"))
    assert usage["input_tokens"] == 26000 * len(pages) and usage["cached_input_tokens"] == 12000 * len(pages)

    res = recognition.import_results(demo_copy, task / "results")
    assert res.imported == len(pages) and not res.errors
    with demo_copy.connect() as con:
        rows = con.execute("SELECT executor, model FROM recognitions WHERE executor='codex'").fetchall()
    assert len(rows) == len(pages) and {r["model"] for r in rows} == {"gpt-6.1-sol"}


def test_strong_tier_sends_tiles_and_custom_model(demo_copy):
    task = recognition.make_task(demo_copy, scope="conflict")
    fake = FakeCodex()
    r = codex.run(task, "strong", model="gpt-6-astra", runner=fake, workers=1)
    assert r.done > 0
    cmd = fake.calls[0]["cmd"]
    assert _arg(cmd, "-m") == "gpt-6-astra" and 'model_reasoning_effort="high"' in cmd
    assert cmd.count("-i") == 2 + 9 and "第 11 张：局部切片 第 3 行第 3 列" in fake.calls[0]["prompt"]


def test_retry_bad_json_and_fatal_errors(demo_copy):
    task = recognition.make_task(demo_copy, scope="new", render=False)
    n_pages = len(json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"])

    def flaky(prompt, n):
        body = _body_from_prompt(prompt)
        if n == 1:
            return "不是 JSON", None
        if n == 2:
            body["page_kind"] = "图"  # 不在枚举里：带着错误重试
        return body, None
    fake = FakeCodex(flaky)
    lines = []
    r = codex.run(task, "economy", runner=fake, workers=1, log_fn=lines.append)
    assert r.done == n_pages - 1 and len(r.failed) == 1 and "不是合法 JSON" in r.failed[0]["error"]
    retry = fake.calls[2]["prompt"]
    assert "上一次的输出没有通过校验" in retry and "page_kind" in retry
    assert any("↻" in ln for ln in lines) and "没有图片" in fake.calls[0]["prompt"]

    # 模型不能用：第一页就停下，提示更新 Codex
    task2 = recognition.make_task(demo_copy, scope="all", render=False)
    blocked = FakeCodex(lambda p, n: (None, "The 'gpt-9' model is not supported when using Codex with a ChatGPT account."))
    with pytest.raises(claude.FatalError, match="npm install -g @openai/codex@latest"):
        codex.run(task2, "economy", model="gpt-9", runner=blocked, workers=3)
    assert len(blocked.calls) == 1
    assert "停止：Codex 不能用模型 gpt-9" in (task2 / "results" / "progress.log").read_text(encoding="utf-8")

    limited = FakeCodex(lambda p, n: (None, "You've hit your usage limit. Try again in 3 hours."))
    with pytest.raises(claude.FatalError, match="额度"):
        codex.run(task2, "economy", runner=limited, workers=3)

    def slow(cmd, prompt, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)
    r = codex.run(task2, "economy", runner=slow, workers=2)
    assert r.done == 0 and r.failed and all("没有完成这一页" in f["error"] for f in r.failed)


def test_find_codex_prefers_newest_and_env_override(monkeypatch, tmp_path):
    versions = {"npm": "codex-cli 0.162.1", "app-old": "codex-cli 0.130.0-alpha.5", "app-new": "codex-cli 0.162.0-alpha.17.2"}
    monkeypatch.setattr(codex, "_candidates", lambda: [["app-old"], ["npm"], ["app-new"]])
    monkeypatch.setattr(codex, "_quick", lambda cmd, timeout=30: subprocess.CompletedProcess(cmd, 0, versions[cmd[0]], ""))
    c = codex.find_codex(refresh=True)
    assert c.cmd == ["npm"] and c.version == "0.162.1"
    assert codex.version_key("0.162.1") > codex.version_key("0.162.0-alpha.17.2") > codex.version_key("0.130.0")

    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    exe = tmp_path / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert codex._npm_native(str(shim)) == [str(exe)]  # 绕开批处理，直接用包里的程序
    monkeypatch.undo()
    monkeypatch.setenv("BIDBENCH_CODEX", str(exe))
    assert codex._candidates() == [[str(exe)]]


@pytest.mark.parametrize("found,login,models,expect", [
    (False, "", [], "没有找到 Codex"),
    (True, "Not logged in", ["gpt-6.1-sol"], "codex login"),
    (True, "Logged in using ChatGPT", ["gpt-5.6-sol"], "请先更新"),
    (True, "Logged in using ChatGPT", ["gpt-6.1-sol", "gpt-6-astra"], None),
])
def test_status_hints(monkeypatch, found, login, models, expect):
    catalog = {"models": [{"slug": m, "display_name": m.upper(), "visibility": "list", "input_modalities": ["text", "image"],
                           "priority": i, "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]}
                          for i, m in enumerate(models)] + [{"slug": "hidden", "visibility": "hide", "input_modalities": ["image"]}]}

    def quick(cmd, timeout=30):
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "codex-cli 0.162.1", "")
        if cmd[-1] == "status":
            return subprocess.CompletedProcess(cmd, 0 if login.startswith("Logged") else 1, login, "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(catalog), "")
    monkeypatch.setattr(codex, "_candidates", lambda: [["codex"]] if found else [])
    monkeypatch.setattr(codex, "_quick", quick)
    st = codex.status(refresh=True)
    assert st["ready"] == (found and login.startswith("Logged"))
    assert (st["hint"] is None) if expect is None else (expect in st["hint"])
    if found:
        assert [m["slug"] for m in st["models"]] == models and st["models"][0]["efforts"] == ["low", "high"]


def _fake_status(ready=True):
    return {"found": True, "ready": ready, "version": "0.162.1", "logged_in": ready, "default_model": "gpt-6.1-sol",
            "models": [{"slug": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "description": "", "efforts": ["low"]}],
            "hint": None if ready else "Codex 还没有登录：在命令行运行 codex login，用 ChatGPT 账号登录。"}


def test_workbench_codex_flow(demo_copy, monkeypatch):
    import threading
    from test_desktop import Client
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: _fake_status())
    monkeypatch.setattr(ops, "recognize_codex", functools.partial(ops.recognize_codex, runner=FakeCodex()))
    blank = FakeCodex(lambda p, n: ({"sheets": [], "facts": []}, None))  # 识别完的智能分类另有测试（test_classify）
    monkeypatch.setattr(ops, "classify", functools.partial(ops.classify, runner=blank))
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        info = c.get("/api/recognizers")
        assert info["codex"]["ready"] and info["claude"]["defaults"]["economy"] == "claude-haiku-5-5"
        assert "注入" not in str(c.post("/api/job", {"kind": "estimate", "executor": "codex", "model": "--注入"}))
        assert c.post("/api/job", {"kind": "estimate", "executor": "codex", "model": "--config=x"})["_status"] == 400
        est = c.job("estimate", executor="codex", scope="conflict", tier="economy", model="gpt-6.1-sol")
        assert est["status"] == "done" and est["title"] == "识别估算（Codex 经济档）", est
        res = est["result"]
        assert res["executor"] == "codex" and res["minutes"] >= 1 and "不另外花钱" in "\n".join(est["lines"])
        assert c.get("/api/recognizers")["prefs"] == {"executor": "codex", "scope": "conflict", "tier": "economy",
                                                     "model": "gpt-6.1-sol"}
        assert "task" not in res and res["priorities"]  # 估算不再生成任务包，只给出分档计划
        assert "选一档" in c.post("/api/job", {"kind": "recognize", "executor": "codex", "priorities": []})["error"]
        assert "选一步" in c.post("/api/job", {"kind": "recognize", "executor": "codex", "stages": ["xx"]})["error"]
        assert res["stages"] and res["map"]["pages"] > 0  # 分步识别：工作台按步勾选
        j = c.job("recognize", executor="codex", scope=res["scope"], tier="economy", model=res["model"],
                  stages=[st["key"] for st in res["stages"] if st["left"]])
        assert j["status"] == "done" and j["title"] == "Codex 识别（经济档）", j
        assert any("✓" in ln for ln in j["lines"]) and j["result"]["imported"] > 0 and j["imported"] > 0
        monkeypatch.setattr(ops, "codex_status", lambda refresh=False: _fake_status(ready=False))
        assert "codex login" in c.post("/api/job", {"kind": "recognize", "executor": "codex", "scope": "conflict"})["error"]
    finally:
        httpd.shutdown()
    assert [e["title"] for e in oplog.read(demo_copy)][:2] == ["Codex 识别（经济档）", "识别估算（Codex 经济档）"]


def test_cli_codex_dry_run(demo_copy, capsys):
    cli.main(["-p", str(demo_copy.root), "recognize", "--executor", "codex", "--scope", "conflict", "--dry-run"])
    out = capsys.readouterr().out
    assert "用 Codex（模型 gpt-6.1-sol，思考强度 medium）" in out and "预计约" in out
    assert oplog.read(demo_copy)[0]["title"] == "识别估算（Codex 经济档）"
    with pytest.raises(SystemExit, match="ultra 只有 Codex"):
        cli.main(["-p", str(demo_copy.root), "recognize", "--executor", "claude", "--effort", "ultra", "--dry-run"])
