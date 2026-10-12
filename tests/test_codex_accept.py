"""Codex 验收脚本（tools/codex_accept.py）里不调用 Codex 的部分：提示、进度行、报告。"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_accept", ROOT / "tools" / "codex_accept.py")
accept = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accept)


def test_prompt_states_the_rules():
    p = accept.build_prompt("abc1234", ["A1", "D1"], r"C:\Program Files\Git\bin\bash.exe")
    assert "提交 abc1234" in p and "只验收这些条目：A1、D1" in p and "docs/验收清单.md" in p
    for rule in ("不要改动任何代码", "只用合成数据", "不要调用 Claude API", "--dry-run", "Git Bash"):
        assert rule in p
    assert "全部条目都要验收" in accept.build_prompt("abc1234", None, None)


def test_progress_lines_and_report():
    cmd = {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0,
                                               "command": '"C:\\pwsh.exe" -Command "python -m pytest -q"'}}
    assert accept.describe(cmd) == "  · python -m pytest -q → 退出码 0"
    final = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"items": []}'}}
    assert accept.describe(final) is None  # 最后交回的 JSON 不当进度显示
    result = {"summary": "大部分通过。", "items": [
        {"id": "A1", "title": "全部测试通过", "result": "通过", "evidence": "pytest -q：158 passed", "problems": "无"},
        {"id": "E1", "title": "本机接口和令牌", "result": "不通过", "evidence": "POST 返回 200", "problems": "没带令牌也能改 | 复现"},
        {"id": "F2", "title": "Codex 估算", "result": "无法验证", "evidence": "", "problems": "没有找到 Codex"}]}
    meta = {"commit": "abc1234", "started": "2026-10-12 18:00", "minutes": 12.5, "codex": "0.162.1", "model": "gpt-6.1-sol",
            "effort": "medium", "input_tokens": 1000, "output_tokens": 200, "checks": ["副本里的代码没有被改动。"]}
    md = accept.report_markdown(result, meta)
    assert "通过 1 项，不通过 1 项，无法验证 1 项" in md and "| A1 全部测试通过 | ✓ 通过 |" in md
    assert r"没带令牌也能改 \| 复现" in md and "副本里的代码没有被改动" in md
    schema_items = accept.SCHEMA["properties"]["items"]["items"]
    assert schema_items["required"] == list(schema_items["properties"])  # Codex 结构化输出要求全部必填
