"""本机用户设置：最近打开的项目、Claude API 密钥、下载加速前缀。

存在用户目录（Windows 为 %APPDATA%\\bidbench\\config.json），不在程序目录里，更新和重装都不会丢。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_RECENT = 12


def config_dir() -> Path:
    if os.environ.get("BIDBENCH_CONFIG_DIR"):
        d = Path(os.environ["BIDBENCH_CONFIG_DIR"])
    elif os.name == "nt":
        d = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "bidbench"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "bidbench"
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bidbench"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return config_dir() / "config.json"


def load() -> dict:
    try:
        cfg = json.loads(_path().read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(cfg: dict) -> None:
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _path())


def add_recent(path: Path, name: str) -> None:
    cfg = load()
    p = str(Path(path).resolve())
    rows = [r for r in cfg.get("recent_projects", []) if r.get("path") != p]
    cfg["recent_projects"] = [{"path": p, "name": name}, *rows][:MAX_RECENT]
    cfg["last_project"] = p
    save(cfg)


def recent() -> list[dict]:
    out = []
    for r in load().get("recent_projects", []):
        p = Path(r.get("path", ""))
        out.append({"path": str(p), "name": r.get("name") or p.name, "exists": (p / "bidbench.json").exists()})
    return out


def forget(path: str) -> None:
    cfg = load()
    cfg["recent_projects"] = [r for r in cfg.get("recent_projects", []) if r.get("path") != path]
    if cfg.get("last_project") == path:
        cfg.pop("last_project")
    save(cfg)


def apply_secrets(cfg: dict | None = None) -> None:
    """把设置里的 API 密钥交给 Claude 执行器（环境变量里已有的优先）。"""
    cfg = load() if cfg is None else cfg
    key = cfg.get("anthropic_api_key")
    if key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = key


def update_settings(api_key: str | None = None, download_prefix: str | None = None) -> dict:
    """api_key 传空字符串表示删除；None 表示不改。"""
    cfg = load()
    if api_key is not None:
        old = cfg.pop("anthropic_api_key", None)
        if old and os.environ.get("ANTHROPIC_API_KEY") == old:
            os.environ.pop("ANTHROPIC_API_KEY")
        if api_key.strip():
            cfg["anthropic_api_key"] = api_key.strip()
    if download_prefix is not None:
        cfg["download_prefix"] = download_prefix.strip()
    save(cfg)
    apply_secrets(cfg)
    return public_settings(cfg)


def public_settings(cfg: dict | None = None) -> dict:
    """给界面看的设置：密钥只说有没有，不回传。"""
    cfg = load() if cfg is None else cfg
    key = cfg.get("anthropic_api_key") or ""
    return {"has_api_key": bool(key or os.environ.get("ANTHROPIC_API_KEY")),
            "api_key_hint": (key[:7] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else ""),
            "api_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")) and not key,
            "download_prefix": cfg.get("download_prefix", "")}


def remember(key: str, value) -> None:
    """记住界面上的选择（识别方式、模型等），下次打开时还是这样。"""
    cfg = load()
    cfg.setdefault("prefs", {})[key] = value
    save(cfg)


def prefs() -> dict:
    p = load().get("prefs")
    return p if isinstance(p, dict) else {}


def default_projects_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.exists() else Path.home()) / "bidbench 项目"
