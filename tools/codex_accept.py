"""让 Codex 按 docs/验收清单.md 验收 bidbench：只验证，不修改代码。

用法（在仓库根目录，用项目的虚拟环境）：

    .venv\\Scripts\\python tools\\codex_accept.py                 # 全部条目
    .venv\\Scripts\\python tools\\codex_accept.py --items A1,D1   # 只验部分条目
    选项：--model（默认 gpt-6.1-sol）、--effort（默认 medium）、--keep（保留临时副本，便于排查）

做法：

1. 用 git worktree 把当前提交检出到一个临时副本。未提交的改动不在验收范围内（会提示）。
2. 在副本里运行 codex exec：Codex 按清单实际操作，最后按格式交回每一项的结果。给 Codex 的环境：
   - PATH 最前面是项目的虚拟环境，PYTHONPATH 指向副本，所以跑的是副本里的代码；
   - bidbench 的用户设置换成副本里的临时目录：碰不到你的真实项目和 API 密钥；
   - 去掉 Claude 密钥（不会花钱）；BIDBENCH_CODEX 指向不存在的路径（bidbench 不会再去调用真的 Codex）。
   这台电脑上 Codex 的 Windows 沙箱启动不了命令（实测两种模式都失败），所以用完全权限运行；
   结束后检查你的代码目录和副本里的代码有没有被改动，有就写进报告。
3. 报告写到 验收报告/<日期时间>-<提交>.md，旁边是 Codex 的完整事件记录 .jsonl；终端打印汇总。
   退出码：没有不通过的 0（“无法验证”的条目在报告里列出），有不通过的 1，没能完成 2。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ("通过", "不通过", "无法验证")
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items", "summary"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "title", "result", "evidence", "problems"],
            "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "result": {"type": "string", "enum": list(RESULTS)},
                           "evidence": {"type": "string"}, "problems": {"type": "string"}}}},
        "summary": {"type": "string"}},
}


def git(*args, cwd=ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


def build_prompt(commit: str, items: list[str] | None, bash: str | None) -> str:
    which = f"只验收这些条目：{'、'.join(items)}。" if items else "全部条目都要验收。"
    shell = f"运行 .sh 脚本用 Git Bash：{bash}。" if bash else ""
    return f"""你是 bidbench（投标图纸工作台）的验收员。当前目录是项目代码的一个临时副本（提交 {commit}）。
请按 docs/验收清单.md 逐项实际操作、核对结果。{which}

规则：
- 只验证，不修改：不要改动任何代码、文档、测试，不要 git commit、git push。发现问题写进结果，不要自己去修。
- 只用合成数据：图纸只用 samples/make_sample.py 生成的样例，不要打开任何真实项目或真实图纸。
- 不花钱、不占额度：不要调用 Claude API；不要真的用 Codex 识图，只做 --dry-run 估算。
- 临时文件都放在 .accept-tmp/ 下；启动的服务（bidbench serve）测完要关掉。
- 环境已经准备好：python 和 bidbench 命令用的是项目的虚拟环境，运行的是这个副本里的代码。{shell}
- 每一项：result 只能是 通过 / 不通过 / 无法验证；evidence 写运行的命令和关键输出（简短）；
  problems 写清楚现象和怎么复现，没有就写“无”。

最后按给定的输出格式交回每一项的结果，summary 用一两句话总结。"""


def short(cmd: str, n: int = 110) -> str:
    """Codex 在 Windows 上用 pwsh -Command 包一层，显示时去掉。"""
    c = cmd
    if "-Command" in c:
        c = c.split("-Command", 1)[1].strip().strip('"')
    c = " ".join(c.split())
    return c if len(c) <= n else c[:n - 1] + "…"


def describe(event: dict) -> str | None:
    """把 Codex 的事件变成一行进度。"""
    it = event.get("item") or {}
    if event.get("type") == "item.completed" and it.get("type") == "command_execution":
        return f"  · {short(it.get('command') or '')} → 退出码 {it.get('exit_code')}"
    if event.get("type") == "item.completed" and it.get("type") == "agent_message":
        text = (it.get("text") or "").strip().splitlines()
        return f"  Codex：{text[0][:120]}" if text and not text[0].startswith("{") else None
    if event.get("type") in ("error", "turn.failed"):
        return f"  ✗ {json.dumps(event, ensure_ascii=False)[:200]}"
    return None


def report_markdown(result: dict, meta: dict) -> str:
    items = result.get("items") or []
    count = {r: sum(1 for i in items if i.get("result") == r) for r in RESULTS}
    mark = {"通过": "✓", "不通过": "✗", "无法验证": "？"}
    cell = lambda s: (s or "").replace("|", "\\|").replace("\n", "<br>")  # noqa: E731
    lines = [f"# 验收报告（提交 {meta['commit']}）", "",
             f"- 时间：{meta['started']} 起，用时 {meta['minutes']} 分钟",
             f"- 验收员：Codex {meta['codex']}，模型 {meta['model']}，思考强度 {meta['effort']}",
             f"- 用量：输入 {meta['input_tokens']} tokens，输出 {meta['output_tokens']} tokens（ChatGPT 账号的 Codex 额度）",
             f"- 结果：通过 {count['通过']} 项，不通过 {count['不通过']} 项，无法验证 {count['无法验证']} 项", "",
             f"**总结**：{result.get('summary') or '（没有总结）'}", "",
             "| 条目 | 结果 | 证据 | 问题 |", "| --- | --- | --- | --- |"]
    lines += [f"| {cell(i.get('id'))} {cell(i.get('title'))} | {mark.get(i.get('result'), '')} {cell(i.get('result'))} "
              f"| {cell(i.get('evidence'))} | {cell(i.get('problems'))} |" for i in items]
    lines += ["", "## 检查", ""] + [f"- {x}" for x in meta["checks"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="让 Codex 按 docs/验收清单.md 验收（只验证，不修改代码）")
    ap.add_argument("--items", help="只验这些条目，逗号分隔，如 A1,D1")
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--keep", action="store_true", help="保留临时副本，便于排查")
    ap.add_argument("--out", default=str(ROOT / "验收报告"), help="报告放在哪里")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    sys.path.insert(0, str(ROOT))
    from bidbench.executors import codex as cx
    st = cx.status(refresh=True)
    if not st["ready"]:
        print(st["hint"])
        return 2
    found = cx.find_codex()
    commit = git("rev-parse", "--short", "HEAD")
    before = git("status", "--porcelain")
    if before:
        print(f"注意：有未提交的改动，这次验收的是提交 {commit}，不包括这些改动。")
    items = [x.strip().upper() for x in args.items.split(",")] if args.items else None

    tmp = Path(tempfile.mkdtemp(prefix="bidbench-accept-"))
    work = tmp / "repo"
    git("worktree", "add", "--detach", str(work), "HEAD")
    scratch = work / ".accept-tmp"
    (scratch / "tmp").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update({"PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""), "PYTHONPATH": str(work),
                "PYTHONUTF8": "1", "BIDBENCH_CONFIG_DIR": str(scratch / "config"),
                "BIDBENCH_CODEX": str(scratch / "no-codex.exe"), "TEMP": str(scratch / "tmp"), "TMP": str(scratch / "tmp")})
    bash = shutil.which("bash") or next((p for p in (r"C:\Program Files\Git\bin\bash.exe",) if Path(p).exists()), None)
    schema_path, out_path = tmp / "schema.json", tmp / "result.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    cmd = [*found.cmd, "exec", "--ignore-user-config", "--sandbox", "danger-full-access", "--skip-git-repo-check",
           "--ephemeral", "--json", "--color", "never", "-C", str(work), "-m", args.model,
           "-c", f'model_reasoning_effort="{args.effort}"', "-c", 'approval_policy="never"',
           "--output-schema", str(schema_path), "-o", str(out_path), "-"]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    base = out_dir / f"{stamp:%Y%m%d-%H%M}-{commit}"
    print(f"验收提交 {commit}：Codex {found.version}，模型 {args.model}，思考强度 {args.effort}。副本：{work}")
    t0 = time.monotonic()
    usage = {"input_tokens": 0, "output_tokens": 0}
    code = 2
    try:
        with open(base.with_suffix(".jsonl"), "w", encoding="utf-8") as events:
            p = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            p.stdin.write(build_prompt(commit, items, bash))
            p.stdin.close()
            try:
                for ln in p.stdout:
                    events.write(ln)
                    try:
                        e = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") == "turn.completed":
                        for k in usage:
                            usage[k] += (e.get("usage") or {}).get(k, 0)
                    line = describe(e)
                    if line:
                        print(line, flush=True)
                p.wait()
            except KeyboardInterrupt:
                p.kill()
                print("已中断。")
                return 2
        if not out_path.exists():
            print(f"Codex 没有交回结果（返回码 {p.returncode}）。事件记录：{base.with_suffix('.jsonl')}")
            return 2
        result = json.loads(out_path.read_text(encoding="utf-8"))
        changed = git("status", "--porcelain", "--untracked-files=no", cwd=work)
        checks = ["副本里的代码没有被改动。" if not changed else f"Codex 改动了副本里的这些文件（已丢弃）：{changed}",
                  "你的代码目录没有被改动。" if git("status", "--porcelain") == before
                  else "你的代码目录在验收期间有变化，请检查（也可能是你自己在改）。"]
        meta = {"commit": commit, "started": f"{stamp:%Y-%m-%d %H:%M}", "minutes": round((time.monotonic() - t0) / 60, 1),
                "codex": found.version, "model": args.model, "effort": args.effort, "checks": checks, **usage}
        report = base.with_suffix(".md")
        report.write_text(report_markdown(result, meta), encoding="utf-8")
        bad = [i for i in result.get("items") or [] if i.get("result") != "通过"]
        print(f"\n{result.get('summary', '')}")
        for i in result.get("items") or []:
            print(f"  {'✓' if i.get('result') == '通过' else '✗' if i.get('result') == '不通过' else '？'} "
                  f"{i.get('id')} {i.get('title')}：{i.get('result')}" + (f"——{i.get('problems')}" if i.get("result") != "通过" else ""))
        for c in checks:
            print(f"  {c}")
        print(f"报告：{report}")
        code = 1 if any(i.get("result") == "不通过" for i in bad) else 0
        return code
    finally:
        if args.keep:
            print(f"临时副本保留在：{work}")
        else:
            subprocess.run(["git", "worktree", "remove", "--force", str(work)], cwd=ROOT, capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
