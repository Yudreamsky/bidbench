"""项目目录与数据库。

一个项目对应一个文件夹：

    <项目>/
      bidbench.json         项目信息
      bidbench.sqlite       原始登记数据 + 人工决定（派生结果每次重新计算）
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
CREATE TABLE IF NOT EXISTS baselines(
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  as_of_batch INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  note TEXT
);
"""


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
        con.executescript(SCHEMA_SQL)
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


def latest_decisions(con: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    """同一 (kind, key) 以最后一次决定为准。"""
    out: dict[tuple[str, str], sqlite3.Row] = {}
    for row in con.execute("SELECT * FROM decisions ORDER BY id"):
        out[(row["kind"], row["key"])] = row
    return out
