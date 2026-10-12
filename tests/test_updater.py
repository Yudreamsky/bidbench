"""一键更新与数据库迁移。用临时 git 仓库模拟“开发者推送 → 用户更新”。"""

import json
import os
import sqlite3
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from bidbench import store, updater
from bidbench.server import serve
from bidbench.store import Project

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def commit(repo: Path, files: dict, message: str, push: bool = True):
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    if push:
        git(repo, "push", "-q", "origin", "main")


@pytest.fixture
def repos(tmp_path):
    up = tmp_path / "up.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(up))
    dev = tmp_path / "dev"
    git(tmp_path, "clone", "-q", str(up), str(dev))
    git(dev, "checkout", "-q", "-b", "main")
    commit(dev, {"pyproject.toml": "[project]\nname='x'\n", "app.py": "v = 1\n", ".gitignore": "projects/\n"}, "初始版本")
    user = tmp_path / "user"
    git(tmp_path, "clone", "-q", str(up), str(user))
    return dev, user


def test_check_and_apply(repos):
    dev, user = repos
    assert updater.check(user)["status"] == "up_to_date"
    commit(dev, {"app.py": "v = 2\n"}, "楼层覆盖检查")
    commit(dev, {"app.py": "v = 3\n"}, "AI 助手")
    (user / "projects").mkdir()  # 未跟踪 / 被忽略的项目数据不影响更新
    (user / "projects" / "x.sqlite").write_text("data")
    (user / "notes.txt").write_text("未跟踪的文件")
    st = updater.check(user)
    assert st["status"] == "available" and [c["title"] for c in st["commits"]] == ["AI 助手", "楼层覆盖检查"]
    assert st["deps_changed"] is False
    calls = []
    r = updater.apply(user, run_pip=calls.append)
    assert (user / "app.py").read_text() == "v = 3\n" and calls == [] and r["pip_command"] is None
    assert (user / "projects" / "x.sqlite").read_text() == "data"
    assert updater.check(user)["status"] == "up_to_date"
    assert updater.version_info(user)["can_rollback"] is True


def test_dependencies_and_rollback(repos):
    dev, user = repos
    commit(dev, {"pyproject.toml": "[project]\nname='x'\ndependencies=['y']\n", "app.py": "v = 2\n"}, "加依赖")
    st = updater.check(user)
    assert st["deps_changed"] is True
    calls = []
    r = updater.apply(user, run_pip=calls.append)
    assert r["pip_done"] and calls[0][:4] == [updater.sys.executable, "-m", "pip", "install"]
    rb = updater.rollback(user, run_pip=calls.append)
    assert (user / "app.py").read_text() == "v = 1\n" and len(calls) == 2  # 回退也补装依赖
    assert rb["rolled_back_to"] == r["previous"]
    with pytest.raises(updater.UpdateError, match="没有更新记录"):
        updater.rollback(user)
    assert updater.check(user)["status"] == "available"  # 回退后又能看到新版本


def test_refuses_dirty_branch_and_diverged(repos):
    dev, user = repos
    commit(dev, {"app.py": "v = 2\n"}, "新功能")
    (user / "app.py").write_text("我改过的代码\n")
    st = updater.check(user)
    assert st["status"] == "dirty" and "app.py" in st["message"]
    with pytest.raises(updater.UpdateError, match="本地改过代码"):
        updater.apply(user)
    git(user, "checkout", "-q", "--", "app.py")

    git(user, "checkout", "-q", "-b", "试验")
    assert updater.check(user)["status"] == "not_on_main"
    git(user, "checkout", "-q", "main")

    commit(user, {"local.txt": "本地提交"}, "本地提交", push=False)
    st = updater.check(user)
    assert st["status"] == "diverged"
    with pytest.raises(updater.UpdateError, match="分叉"):
        updater.apply(user)


def test_fetch_failure_is_explained(repos, tmp_path):
    _, user = repos
    git(user, "remote", "set-url", "origin", str(tmp_path / "不存在.git"))
    st = updater.check(user)
    assert st["status"] == "error" and st["message"]


def test_not_a_git_install(monkeypatch):
    monkeypatch.setattr(updater, "repo_root", lambda: None)
    st = updater.check()
    assert st["status"] == "not_git" and "git clone" in st["message"]
    with pytest.raises(updater.UpdateError, match="无法回退"):
        updater.rollback()


def test_schema_migration_with_backup(tmp_path, monkeypatch):
    proj = Project.init(tmp_path / "p", "迁移")
    with proj.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        con.execute("INSERT INTO notes(topic, text, source, created_at) VALUES ('a','b','c','d')")
    monkeypatch.setattr(store, "SCHEMA_VERSION", store.SCHEMA_VERSION + 1)
    monkeypatch.setattr(store, "MIGRATIONS", {store.SCHEMA_VERSION: "ALTER TABLE notes ADD COLUMN pinned INTEGER DEFAULT 0;"})
    with proj.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        assert con.execute("SELECT pinned FROM notes").fetchone()[0] == 0
    backups = list(proj.root.glob("bidbench.sqlite.bak-v*"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    assert old.execute("SELECT text FROM notes").fetchone()[0] == "b"  # 备份里是升级前的数据
    old.close()


def test_schema_too_new(tmp_path):
    proj = Project.init(tmp_path / "p", "新版")
    con = sqlite3.connect(proj.db_path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with pytest.raises(store.SchemaTooNew, match="bidbench update"):
        proj.connect()


def test_workbench_update_endpoints(demo_copy, monkeypatch):
    fake = {"status": "available", "message": "有 1 项更新。", "commits": [{"sha": "abc1234", "date": "2026-10-12", "title": "更新"}],
            "deps_changed": False, "current": {"version": "0.1.0", "commit": "0000000"}}
    monkeypatch.setattr(updater, "check", lambda *a, **k: fake)
    monkeypatch.setattr(updater, "apply", lambda *a, **k: {"updated_to": "abc1234", "previous": "0000000",
                                                           "pip_command": None, "pip_done": False, "commits": []})
    httpd = serve(demo_copy, port=0)
    restarted = []
    httpd.app.restart_after = restarted.append
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert json.loads(urllib.request.urlopen(base + "/api/update/check").read())["status"] == "available"
        v = json.loads(urllib.request.urlopen(base + "/api/version").read())
        assert "version" in v
        req = urllib.request.Request(base + "/api/update/apply", method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json", "X-Bidbench-Token": httpd.app.token})
        r = json.loads(urllib.request.urlopen(req).read())
        assert r["restarting"] is True and restarted == [None]
    finally:
        httpd.shutdown()
