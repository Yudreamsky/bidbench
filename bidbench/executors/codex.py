"""Codex 执行器：用本机的 Codex 命令行（codex exec）识别图纸页面。

用的是你在 Codex 里登录的 ChatGPT 账号：不另外计费，占用账号的 Codex 用量额度。

每页单独调用一次 codex exec：附上整页缩略图和图签放大图（强档另加 3×3 局部切片），提示里是和 Claude 执行器
同一份识别要求（claude.SYSTEM_PROMPT）和这一页的文字层，按给定格式输出 JSON。结果在本地用完整 schema 校验，
没通过就带着错误重试一次。续跑、先试第一页、并发、逐页记录（results/progress.log）都和 Claude 执行器共用一套流程。

安全：Codex 在只读沙箱里运行（--sandbox read-only），不读你的 Codex 设置（--ignore-user-config，
不带上设置里的全权限、插件、MCP 和通知），不保存会话记录（--ephemeral），工作目录是只放了这一页图片的临时文件夹。
图纸页面会发送给 OpenAI。执行器只读任务包、只写 results/。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jsonschema

from ..debuglog import tail
from ..proc import NO_WINDOW
from ..recognition import load_schema
from . import claude
from .claude import BODY_KEYS, FatalError, ProgressLog, RecognitionError, RunResult, run_pages

log = logging.getLogger(__name__)

EXECUTOR = "codex"
DEFAULT_MODEL = "gpt-6.1-sol"
TIERS = {
    "economy": {"model": DEFAULT_MODEL, "effort": "medium", "tiles": 0},
    "strong": {"model": DEFAULT_MODEL, "effort": "high", "tiles": 3},
}
DEFAULT_WORKERS = 3
MAX_IMAGE_EDGE = 2048          # 图片长边缩到这个尺寸以内再交给 Codex
PAGE_TIMEOUT = 900             # 一页最多等 15 分钟
SECONDS_PER_PAGE = 45          # 估算用时（实测一页 30~60 秒）
INSTALL_HINT = "npm install -g @openai/codex@latest"
OUTPUT_RULE = "只输出这一页的识别结果 JSON，格式见给定的输出格式。这是一次识别任务：不要读写任何文件，不要运行任何命令。"


# ---------------- 找到本机的 Codex ----------------

@dataclass
class Codex:
    cmd: list[str]   # 启动命令：可执行文件，或 node + codex.js
    version: str     # 如 0.162.1


_VER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-[A-Za-z]+\.?(\d+))?")


def version_key(v: str) -> tuple:
    """版本比较：0.162.1 比 0.162.0-alpha.17 新。"""
    m = _VER.search(v or "")
    if not m:
        return (-1,)
    major, minor, patch, pre = m.groups()
    return int(major), int(minor), int(patch), 0 if pre is not None else 1, int(pre or 0)


def _npm_native(shim: str) -> list[str] | None:
    """npm 装的 codex 在 Windows 上是批处理 codex.cmd，经批处理转发中文路径等参数容易出错，直接用包里的 codex.exe。"""
    root = Path(shim).parent / "node_modules" / "@openai" / "codex"
    for exe in sorted(root.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe")):
        return [str(exe)]
    js, node = root / "bin" / "codex.js", shutil.which("node")
    return [node, str(js)] if js.exists() and node else None


def _candidates() -> list[list[str]]:
    if os.environ.get("BIDBENCH_CODEX"):  # 指定了就只用它
        return [[os.environ["BIDBENCH_CODEX"]]]
    out: list[list[str]] = []
    p = shutil.which("codex")
    if p:
        cmd = _npm_native(p) if p.lower().endswith((".cmd", ".bat", ".ps1")) else [p]
        if cmd:
            out.append(cmd)
    if os.name == "nt":  # Codex 桌面版自带的命令行
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "OpenAI" / "Codex" / "bin"
        out += [[str(x)] for x in sorted(base.glob("*/codex.exe"))]
        if (base / "codex.exe").exists():
            out.append([str(base / "codex.exe")])
    seen, uniq = set(), []
    for c in out:
        if tuple(c) not in seen:
            seen.add(tuple(c))
            uniq.append(c)
    return uniq


def _quick(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("Codex 命令没能运行：%s：%s", cmd[:2], e)
        return None


_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 60


def _cached(key, fn, refresh: bool):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def find_codex(refresh: bool = False) -> Codex | None:
    """本机能用的 Codex：PATH 里的（npm 安装）和 Codex 桌面版自带的，取版本最新的；环境变量 BIDBENCH_CODEX 可以指定。"""
    def look():
        found = []
        for c in _candidates():
            r = _quick([*c, "--version"])
            m = _VER.search(r.stdout) if r and r.returncode == 0 else None
            if m:
                found.append(Codex(c, m.group(0)))
        best = max(found, key=lambda c: version_key(c.version), default=None)
        log.info("Codex：%s", f"{best.version}（{best.cmd[-1]}）" if best else "没有找到")
        return best
    return _cached("codex", look, refresh)


def list_models(codex: Codex) -> list[dict]:
    """账号能用、能看图的模型（codex debug models），按 Codex 推荐的顺序。"""
    r = _quick([*codex.cmd, "debug", "models"])
    try:
        rows = json.loads(r.stdout)["models"] if r and r.returncode == 0 else []
    except (json.JSONDecodeError, KeyError, TypeError):
        rows = []
    rows = [m for m in rows if isinstance(m, dict) and m.get("visibility") == "list"
            and "image" in (m.get("input_modalities") or [])]
    rows.sort(key=lambda m: m["priority"] if isinstance(m.get("priority"), (int, float)) else 99)
    return [{"slug": m["slug"], "name": m.get("display_name") or m["slug"], "description": m.get("description") or "",
             "efforts": [x.get("effort") for x in m.get("supported_reasoning_levels") or [] if x.get("effort")]}
            for m in rows]


def status(refresh: bool = False) -> dict:
    """给工作台看的 Codex 状态：装没装、什么版本、登录没有、能用哪些模型。"""
    def look():
        c = find_codex(refresh)
        if c is None:
            return {"found": False, "ready": False, "models": [], "default_model": DEFAULT_MODEL,
                    "hint": f"没有找到 Codex。安装：{INSTALL_HINT}，再在命令行运行 codex login，用 ChatGPT 账号登录。"}
        r = _quick([*c.cmd, "login", "status"])
        logged_in = bool(r and r.returncode == 0 and "logged in" in (r.stdout + r.stderr).lower()
                         and "not logged in" not in (r.stdout + r.stderr).lower())
        models = list_models(c)
        hint = None
        if not logged_in:
            hint = "Codex 还没有登录：在命令行运行 codex login，用 ChatGPT 账号登录。"
        elif models and DEFAULT_MODEL not in [m["slug"] for m in models]:
            hint = f"这个版本的 Codex（{c.version}）不认识 {DEFAULT_MODEL}，请先更新：{INSTALL_HINT}"
        return {"found": True, "ready": logged_in, "version": c.version, "path": c.cmd[-1], "logged_in": logged_in,
                "models": models, "default_model": DEFAULT_MODEL, "hint": hint}
    return _cached("status", look, refresh)


# ---------------- 输出格式 ----------------

def output_schema() -> dict:
    """OpenAI 结构化输出（严格模式）的格式：每个对象的字段都要列为必填，$ref 旁边不能有别的关键字。
    完整格式里可选的字段本来都允许为 null（有测试保证），所以都列为必填不改变含义。"""
    def strict(n):
        if isinstance(n, list):
            return [strict(x) for x in n]
        if not isinstance(n, dict):
            return n
        if "$ref" in n:
            return {"$ref": n["$ref"]}
        out = {k: strict(v) for k, v in n.items()}
        if isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    return strict(claude.body_schema())


def build_prompt(task_dir: Path, item: dict, notes: list[str]) -> str:
    pics = "\n".join(f"第 {i} 张：{n}" for i, n in enumerate(notes, 1)) or "（这一页没有图片，只能依靠文字层）"
    return "\n\n".join([claude.SYSTEM_PROMPT, claude.page_header(item), "附图：\n" + pics,
                        claude.text_layer(task_dir, item), OUTPUT_RULE])


# ---------------- 调用 ----------------

def _subprocess_runner(cmd: list[str], prompt: str, timeout: int) -> tuple[int, str, str]:
    r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, **NO_WINDOW)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def _message(raw: str) -> str:
    """Codex 转述的接口错误常常是一段 JSON，取出里面的 message。"""
    try:
        d = json.loads(raw)
        while isinstance(d, dict) and isinstance(d.get("error"), dict):
            d = d["error"]
        if isinstance(d, dict) and d.get("message"):
            return str(d["message"])
    except (json.JSONDecodeError, TypeError):
        pass
    return raw.strip()


def classify(msg: str, model: str) -> str | None:
    """整次运行都会失败的错误：返回给人看的原因，停下来；其余只算这一页失败。"""
    m = msg.lower()
    if ("model" in m and ("not supported" in m or "does not exist" in m or "not found" in m)) or "model_not_found" in m:
        return f"Codex 不能用模型 {model}：{msg}。可以换一个模型；新模型需要先更新 Codex：{INSTALL_HINT}"
    if "invalid_json_schema" in m or "invalid schema" in m:
        return f"Codex 不接受这份输出格式：{msg}"
    if "not logged in" in m or "unauthorized" in m or re.search(r"\b401\b", m) or "authentication" in m:
        return "Codex 还没有登录或登录已过期：在命令行运行 codex login 后重试"
    if "usage limit" in m or "rate limit" in m or re.search(r"\b429\b", m) or "quota" in m:
        return f"Codex 的用量额度暂时用完了（{msg}）。等额度恢复后，用同一个任务包续跑，已识别的页面会跳过"
    return None


@dataclass
class Call:
    text: str | None
    usage: dict
    error: str | None


def _call(cmd_prefix, runner, workdir: Path, model: str, effort: str, schema_path: Path, images: list[Path],
          prompt: str) -> Call:
    out = workdir / "last-message.json"
    out.unlink(missing_ok=True)
    cmd = [*cmd_prefix, "exec", "--ignore-user-config", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
           "--json", "--color", "never", "-C", str(workdir), "-m", model, "-c", f'model_reasoning_effort="{effort}"',
           "--output-schema", str(schema_path), "-o", str(out)]
    for img in images:
        cmd += ["-i", str(img)]
    cmd.append("-")  # 提示从标准输入读，避免命令行过长和转义问题
    try:
        code, stdout, stderr = runner(cmd, prompt, PAGE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RecognitionError(f"Codex 超过 {PAGE_TIMEOUT // 60} 分钟没有完成这一页", "超时") from e
    usage, errors = {}, []
    for ln in stdout.splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "turn.completed":
            usage = e.get("usage") or {}
        elif e.get("type") == "error":
            errors.append(_message(str(e.get("message", ""))))
        elif e.get("type") == "turn.failed":
            errors.append(_message(str((e.get("error") or {}).get("message", ""))))
    text = out.read_text(encoding="utf-8") if out.exists() else None
    if code != 0 or (errors and not text):
        return Call(text, usage, errors[-1] if errors else (tail(stderr, 300) or f"Codex 返回码 {code}"))
    return Call(text, usage, None)


def recognize_page(cmd_prefix, runner, task_dir: Path, item: dict, model: str, effort: str, tiles: int, usage,
                   validator, schema_path: Path, note=None) -> dict:
    with tempfile.TemporaryDirectory(prefix="bidbench-codex-") as tmp:
        tmp = Path(tmp)
        images, notes = [], []
        for i, (image, frac, desc) in enumerate(claude.page_images(item, tiles), 1):
            p = tmp / f"{i:02d}.png"
            p.write_bytes(claude.png_bytes(task_dir / image, frac, MAX_IMAGE_EDGE))
            images.append(p)
            notes.append(desc)
        prompt = build_prompt(task_dir, item, notes)
        last_error = None
        for attempt in range(2):  # 第二次只在本地校验失败时，带着错误重来
            if attempt and note:
                note(f"  ↻ {claude.page_label(item)}：输出没有通过格式校验，带着错误重试一次")
            call = _call(cmd_prefix, runner, tmp, model, effort, schema_path, images, prompt)
            u = call.usage
            usage.add(SimpleNamespace(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                                      cache_read_input_tokens=u.get("cached_input_tokens", 0), cache_creation_input_tokens=0))
            if call.error:
                fatal = classify(call.error, model)
                if fatal:
                    raise FatalError(fatal)
                raise RecognitionError(f"Codex 出错：{call.error}", "Codex 出错")
            try:
                body = claude._parse_json(call.text or "")
                if not isinstance(body, dict):
                    raise ValueError("不是对象")
            except ValueError as e:  # json.JSONDecodeError 也是 ValueError
                raise RecognitionError(f"Codex 的输出不是合法 JSON：{e}", "输出不是合法 JSON") from e
            body = claude._sanitize({k: body.get(k) for k in BODY_KEYS})
            record = {
                "schema_version": "0.1",
                "page_id": item["page_id"],
                "source": item["source"],
                "run": {"executor": EXECUTOR, "model": model, "instruction_version": claude.INSTRUCTION_VERSION,
                        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                **body,
            }
            errs = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
            if not errs:
                return record
            last_error = "；".join(f"{'/'.join(str(x) for x in e.absolute_path) or '(根)'}: {e.message}" for e in errs[:5])
            prompt = (build_prompt(task_dir, item, notes) + f"\n\n上一次的输出没有通过校验：{last_error}。"
                      f"请输出修正后的完整 JSON。上一次的输出：\n{call.text}")
    raise RecognitionError(f"两次输出都没有通过校验：{last_error}", "两次输出都没有通过格式校验")


def run(task_dir: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
        tiles: int | None = None, workers: int = DEFAULT_WORKERS, progress=None, log_fn=None, runner=None,
        codex: Codex | None = None) -> RunResult:
    """runner(cmd, prompt, timeout) -> (返回码, 标准输出, 标准错误)，测试用假的；不给就调用本机的 Codex。"""
    cfg = TIERS[tier]
    model = model or cfg["model"]
    effort = effort or cfg["effort"]
    tiles = cfg["tiles"] if tiles is None else tiles
    if runner is None:
        codex = codex or find_codex()
        if codex is None:
            raise FatalError(f"没有找到 Codex。安装：{INSTALL_HINT}，再运行 codex login 登录 ChatGPT 账号")
        runner = _subprocess_runner
    cmd_prefix = codex.cmd if codex else ["codex"]
    task_dir = Path(task_dir)
    validator = jsonschema.Draft202012Validator(load_schema())
    note = ProgressLog(task_dir, log_fn)
    with tempfile.TemporaryDirectory(prefix="bidbench-codex-schema-") as tmp:
        schema_path = Path(tmp) / "schema.json"
        schema_path.write_text(json.dumps(output_schema(), ensure_ascii=False), encoding="utf-8")

        def recognize(item, usage, note):
            return recognize_page(cmd_prefix, runner, task_dir, item, model, effort, tiles, usage, validator,
                                  schema_path, note)

        def fatal(e: Exception) -> str | None:
            if isinstance(e, OSError):
                return f"Codex 没能运行：{e}"
            return None

        settings = (f"Codex {codex.version}，" if codex else "Codex，") + f"模型 {model}，思考强度 {effort}" + \
            (f"，局部切片 {tiles}×{tiles}" if tiles > 1 else "")
        res = run_pages(task_dir, recognize, settings, workers, note, model, progress, fatal)
    _write_log(task_dir, res, tier, effort, tiles, codex.version if codex else None)
    return res


def _write_log(task_dir: Path, res: RunResult, tier: str, effort: str, tiles: int, version: str | None) -> None:
    u = res.usage
    lines = ["# 运行记录", "",
             f"执行器：{EXECUTOR}{f'（Codex {version}）' if version else ''}　档位：{tier}　模型：{res.model}　思考强度：{effort}"
             f"　局部切片：{f'{tiles}×{tiles}' if tiles > 1 else '无'}", "",
             f"本次识别 {res.done} 页，跳过已有结果 {res.skipped} 页，失败 {len(res.failed)} 页，用时 {res.seconds:.0f} 秒。", "",
             f"用量：输入 {u.input} tokens（其中缓存 {u.cache_read}），输出 {u.output} tokens。"
             "用的是 ChatGPT 账号的 Codex 额度，不另外计费。"]
    if res.failed:
        lines += ["", "## 失败页面", "", "| 页面 | 文件 | 原因 |", "| --- | --- | --- |"]
        lines += [f"| {x['page_id']} | {x['file']} | {x['error']} |" for x in res.failed]
    else:
        lines += ["", "问题：无"]
    (task_dir / "results" / "run_log.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (task_dir / "results" / "usage.json").write_text(json.dumps({
        "executor": EXECUTOR, "codex_version": version, "model": res.model, "tier": tier, "effort": effort,
        "input_tokens": u.input, "cached_input_tokens": u.cache_read, "output_tokens": u.output,
        "done": res.done, "failed": len(res.failed)}, ensure_ascii=False, indent=2), encoding="utf-8")


def estimate(task_dir: Path, tier: str = "economy", model: str | None = None, tiles: int | None = None,
             workers: int = DEFAULT_WORKERS) -> dict:
    """不调用 Codex 的粗略估算：页数、图片数、大概要多久。"""
    cfg = TIERS[tier]
    tiles = cfg["tiles"] if tiles is None else tiles
    pages = json.loads((Path(task_dir) / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    images = sum(len(claude.page_images(it, tiles)) for it in pages)
    minutes = len(pages) * SECONDS_PER_PAGE * (1.5 if tiles > 1 else 1) / max(1, workers) / 60
    return {"executor": EXECUTOR, "pages": len(pages), "images": images, "model": model or cfg["model"],
            "effort": cfg["effort"], "workers": workers, "minutes": max(1, round(minutes))}
