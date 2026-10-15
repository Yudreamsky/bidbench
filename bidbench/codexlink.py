"""把一个项目接到本机的 Codex（命令行、桌面版、编辑器插件共用 ~/.codex/config.toml）。

写进去的是 [mcp_servers.bidbench] 一节：启动命令用工作台 AI 助手同一套（chat.mcp_command），
另加启动和单次查询的等待时间（大项目第一次算台账要十几秒，Codex 默认只等 10 秒）。
已有的 bidbench 一节整节换掉，其余设置原样保留；改之前把原文件备份成 config.toml.bak。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from . import appconfig
from .store import Project

log = logging.getLogger(__name__)

NAME = "bidbench"
_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def codex_home() -> Path:
    """Codex 的设置目录：设了 CODEX_HOME 就用它，否则是用户目录下的 .codex。"""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_path() -> Path:
    return codex_home() / "config.toml"


def _toml(v) -> str:
    return json.dumps(v, ensure_ascii=False)  # JSON 的字符串和数组写法也是合法的 TOML（反斜杠会转义）


def server_block(proj: Project) -> str:
    from .chat import mcp_command
    cmd = mcp_command(proj)
    env = {"BIDBENCH_CONFIG_DIR": str(appconfig.config_dir()), "PYTHONUTF8": "1"}
    return "\n".join([
        f"# bidbench 项目：{proj.root}（由 bidbench 写入，换项目时整节替换）",
        f"[mcp_servers.{NAME}]",
        f"command = {_toml(cmd[0])}",
        f"args = {_toml(cmd[1:])}",
        "startup_timeout_sec = 60",
        "tool_timeout_sec = 180",
        "env = { " + ", ".join(f"{k} = {_toml(v)}" for k, v in env.items()) + " }",
        "",
    ])


def _ours(table: str) -> bool:
    t = table.replace('"', "").replace("'", "").replace(" ", "")
    return t == f"mcp_servers.{NAME}" or t.startswith(f"mcp_servers.{NAME}.")


def _strip(text: str) -> tuple[str, bool]:
    """去掉已有的 bidbench 一节（连同它的子表和紧挨在上面的那行说明），返回剩下的内容和有没有去掉。"""
    out: list[str] = []
    skipping = found = False
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            skipping = _ours(m.group(1))
            if skipping:
                found = True
                if out and out[-1].startswith("# bidbench 项目："):
                    out.pop()
                continue
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return ("\n".join(out) + "\n" if out else ""), found


def connect(proj: Project, path: Path | None = None) -> dict:
    """写进 Codex 设置，返回写到哪、有没有替换旧的、备份在哪。"""
    path = path or config_path()
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    rest, replaced = _strip(old)
    new = (rest + "\n" if rest else "") + server_block(proj)
    try:
        import tomllib  # Python 3.11+；3.10 上就不校验
    except ImportError:
        tomllib = None
    if tomllib:
        try:
            tomllib.loads(new)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"Codex 的设置文件 {path} 本身有格式错误，先修好再接：{e}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if old:
        backup = path.with_name(path.name + ".bak")
        backup.write_text(old, encoding="utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    log.info("已把项目接到 Codex：替换旧设置=%s", replaced)
    return {"path": str(path), "replaced": replaced, "backup": str(backup) if backup else None, "project": str(proj.root)}


def connected_project(path: Path | None = None) -> str | None:
    """Codex 现在接的是哪个项目（看我们写的那行说明）；没接返回 None。"""
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^# bidbench 项目：(.+?)（由 bidbench 写入", text, re.M)
    if m:
        return m.group(1)
    return "（手动配置）" if _strip(text)[1] else None
