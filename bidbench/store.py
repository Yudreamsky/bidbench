"""项目目录与数据库。

一个项目对应一个文件夹：

    <项目>/
      bidbench.json         项目信息
      bidbench.sqlite       原始登记数据 + 人工决定 + AI 的识别结果和智能分类（派生结果每次重新计算）
      archives/             收到的压缩包原件（只读副本）
      blobs/                解出的全部文件，按 SHA-256 内容寻址存放，天然去重
      recognition/tasks/    识别任务包
      reports/              各类报告（Markdown）
      exports/              现行图纸文件夹、agent 数据镜像
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PLACEHOLDER_EXECUTORS = {"simulated"}  # 只用于演示的执行器；真实执行器的结果出现后，它的结果让位

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batches(
  seq INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  received_at TEXT NOT NULL,
  archive_name TEXT NOT NULL,
  archive_sha256 TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS files(
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  ext TEXT NOT NULL,
  kind TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  page_count INTEGER,
  has_text_layer INTEGER
);
CREATE TABLE IF NOT EXISTS occurrences(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_seq INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  depth INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS occ_sha ON occurrences(sha256);
CREATE TABLE IF NOT EXISTS extraction_issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_seq INTEGER NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages(
  page_id TEXT PRIMARY KEY,
  file_sha256 TEXT NOT NULL,
  page_index INTEGER NOT NULL,
  width REAL NOT NULL,
  height REAL NOT NULL,
  has_text INTEGER NOT NULL,
  render_hash TEXT NOT NULL,
  dhash TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_render ON pages(render_hash);
CREATE TABLE IF NOT EXISTS recognitions(
  page_id TEXT PRIMARY KEY,
  executor TEXT NOT NULL,
  model TEXT NOT NULL,
  instruction_version TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  note TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  outcome TEXT
);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_seq INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  source TEXT NOT NULL,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_sha ON risk_findings(sha256);
CREATE TABLE IF NOT EXISTS av_scans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_seq INTEGER NOT NULL,
  engine TEXT NOT NULL,
  scanned_at TEXT NOT NULL,
  files INTEGER NOT NULL,
  threats INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  message TEXT
);
CREATE TABLE IF NOT EXISTS baselines(
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  as_of_batch INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS classifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  label TEXT NOT NULL,
  executor TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL
);
"""


# 数据库结构版本。修改已有表的结构时：SCHEMA_SQL 写成最新的样子，SCHEMA_VERSION 加 1，
# 并在 MIGRATIONS 里写从上一版升级过来的 SQL。旧项目打开时自动升级，升级前先备份数据库。
# 只新增表（CREATE TABLE IF NOT EXISTS）不用升版本号：旧项目打开时自动建表，回退到旧程序也照样能打开。
SCHEMA_VERSION = 1
MIGRATIONS: dict[int, str] = {}


class SchemaTooNew(ValueError):
    """项目数据库来自更新版本的 bidbench。"""


def migrate(con: sqlite3.Connection, db_path: Path) -> None:
    fresh = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'").fetchone() is None
    if fresh:
        con.executescript(SCHEMA_SQL)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    v = con.execute("PRAGMA user_version").fetchone()[0] or 1  # 加版本号之前的项目都是第 1 版
    if v > SCHEMA_VERSION:
        raise SchemaTooNew(f"这个项目是用更新版本的 bidbench 打开过的（数据库版本 {v}，当前程序支持到 {SCHEMA_VERSION}），"
                           "请先更新程序：bidbench update")
    if v < SCHEMA_VERSION:
        backup = db_path.with_name(f"{db_path.name}.bak-v{v}-{datetime.now():%Y%m%d-%H%M%S}")
        dst = sqlite3.connect(backup)
        try:
            con.backup(dst)
        finally:
            dst.close()
        for k in range(v + 1, SCHEMA_VERSION + 1):
            con.executescript(MIGRATIONS[k])
            con.execute(f"PRAGMA user_version = {k}")
        con.commit()
    elif con.execute("PRAGMA user_version").fetchone()[0] == 0:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Project:
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "bidbench.sqlite"

    @property
    def meta_path(self) -> Path:
        return self.root / "bidbench.json"

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def dir(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        migrate(con, self.db_path)
        con.executescript(SCHEMA_SQL)  # 新增的表直接补建（都是 IF NOT EXISTS）
        return con

    def blob_path(self, sha: str, ext: str) -> Path:
        return self.root / "blobs" / sha[:2] / f"{sha}{ext}"

    def store_blob(self, src: Path, sha: str, ext: str) -> Path:
        """把文件按内容哈希存入 blobs，已存在则跳过。"""
        dst = self.blob_path(sha, ext)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        return dst

    @classmethod
    def init(cls, root: Path, name: str) -> "Project":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        proj = cls(root)
        if proj.meta_path.exists():
            raise FileExistsError(f"{root} 已经是一个项目")
        proj.meta_path.write_text(
            json.dumps({"name": name, "created_at": now_iso(), "format": 1}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        proj.connect().close()
        return proj

    @classmethod
    def open(cls, root: Path) -> "Project":
        proj = cls(Path(root))
        if not proj.meta_path.exists():
            raise FileNotFoundError(f"{root} 不是项目目录，先运行 bidbench init")
        return proj


def add_decision(proj: Project, kind: str, key: str, value: str, note: str | None = None, actor: str = "我") -> None:
    with proj.connect() as con:
        con.execute(
            "INSERT INTO decisions(kind, key, value, note, actor, created_at) VALUES (?,?,?,?,?,?)",
            (kind, key, value, note, actor, now_iso()),
        )


REVOKE = "revoke"  # 撤回决定：也记一条，key 是 JSON [kind, key]；之后这一项算没决定过，问题重新出现


def revoke_key(kind: str, key: str) -> str:
    return json.dumps([kind, key], ensure_ascii=False)


def revoke_decision(proj: Project, kind: str, key: str, note: str | None = None, actor: str = "我") -> None:
    add_decision(proj, REVOKE, revoke_key(kind, key), "", note, actor)


def latest_decisions(con: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    """同一 (kind, key) 以最后一次决定为准；最后一次是撤回的，算没决定。"""
    out: dict[tuple[str, str], sqlite3.Row] = {}
    for row in con.execute("SELECT * FROM decisions ORDER BY id"):
        if row["kind"] == REVOKE:
            try:
                kind, key = json.loads(row["key"])
            except (ValueError, TypeError):
                continue
            out.pop((kind, key), None)
            continue
        out[(row["kind"], row["key"])] = row
    return out
