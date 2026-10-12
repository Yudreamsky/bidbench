"""桌面工作台：项目选择、后台任务、令牌校验、设置、安装版更新（假的 GitHub 接口，不联网）。"""

import hashlib
import io
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bidbench import appconfig, desktop, release, server
from bidbench.server import serve


class Client:
    def __init__(self, httpd):
        self.httpd = httpd
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(self, path):
        return json.loads(urllib.request.urlopen(self.base + path).read())

    def post(self, path, body=None, token=True, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["X-Bidbench-Token"] = self.httpd.app.token
        req = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body or {}).encode(), headers=h)
        try:
            return json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            return {"_status": e.code, **json.loads(e.read())}

    def job(self, kind, timeout=60, **body):
        r = self.post("/api/job", {"kind": kind, **body})
        assert "_status" not in r, r
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.get("/api/job")
            if j["status"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("任务超时")


@pytest.fixture
def running():
    servers = []

    def start(proj, mode="desktop"):
        httpd = serve(proj, port=0, mode=mode)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return Client(httpd)
    yield start
    for s in servers:
        s.shutdown()


def test_token_and_host_are_checked(running, demo_copy):
    c = running(demo_copy)
    html = urllib.request.urlopen(c.base + "/").read().decode()
    assert f'content="{c.httpd.app.token}"' in html  # 令牌只写在本服务返回的页面里
    assert c.post("/api/decision", {"kind": "sheet_void", "key": "建施-05", "value": "voided"}, token=False)["_status"] == 403
    req = urllib.request.Request(c.base + "/api/state", headers={"Host": "evil.example:80"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403  # 防 DNS 重绑定
    assert c.post("/api/decision", {"kind": "sheet_void", "key": "建施-05", "value": "voided"})["as_of"] == 4
    st = c.post("/api/decision", {"kind": "catalog_rev", "key": "建施-06|B", "value": "ask"})
    assert next(i for i in st["issues"] if i["id"] == "catalog_newer:建施-00:建施-06")["confirmed"]


def test_launcher_open_new_and_recent(running, demo_copy, tmp_path):
    c = running(None)
    info = c.get("/api/app")
    assert info["project"] is None and info["mode"] == "desktop"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(c.base + "/api/state")
    assert e.value.code == 409
    assert "不是 bidbench 项目" in c.post("/api/app/open", {"path": str(tmp_path)})["error"]
    info = c.post("/api/app/open", {"path": str(demo_copy.root)})
    assert info["project"]["name"] == "示例综合体项目" and c.get("/api/state")["as_of"] == 4
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x.txt").write_text("x")
    assert "已经有东西" in c.post("/api/app/new", {"path": str(tmp_path / "busy"), "name": "甲"})["error"]
    info = c.post("/api/app/new", {"path": str(tmp_path / "新项目"), "name": "新项目"})
    assert info["project"]["name"] == "新项目" and c.get("/api/state")["batches"] == []
    names = [r["name"] for r in c.get("/api/app")["recent"]]
    assert names[:2] == ["新项目", "示例综合体项目"]
    assert appconfig.load()["last_project"] == str((tmp_path / "新项目").resolve())
    c.post("/api/app/forget", {"path": str(demo_copy.root.resolve())})
    assert [r["name"] for r in c.get("/api/app")["recent"]] == ["新项目"]
    assert c.post("/api/app/close")["project"] is None


def test_jobs_run_the_whole_pipeline(running, sample_zips, tmp_path):
    c = running(None)
    c.post("/api/app/new", {"path": str(tmp_path / "p"), "name": "示例"})
    assert "找不到" in c.post("/api/job", {"kind": "receive", "path": str(tmp_path / "无.zip")})["error"]
    lines = []
    for path, when in sample_zips:
        j = c.job("receive", path=str(path), date=when, av=False)
        assert j["status"] == "done", j["lines"]
        lines += j["lines"]
        assert c.job("recognize", executor="simulated")["status"] == "done"
    assert any("高风险" in ln and "acad.lsp" in ln for ln in lines)
    st = c.get("/api/state")
    assert st["as_of"] == 4 and st["security"]["pending"] == 4
    j = c.job("export", what="current")
    assert j["status"] == "done" and "现行图纸" in j["result"]["path"]
    assert c.job("scan", av=False)["status"] == "done"
    assert c.post("/api/job", {"kind": "export", "what": "其他"})["_status"] == 400


def test_receive_job_takes_folders_and_several_files(running, sample_zips, tmp_path):
    import make_sample
    c = running(None)
    c.post("/api/app/new", {"path": str(tmp_path / "p"), "name": "示例"})
    folder, when = make_sample.build_folder(tmp_path / "in")
    assert "一次只能收一个文件夹" in c.post("/api/job", {"kind": "receive", "paths": [str(folder), str(sample_zips[0][0])]})["error"]
    assert "请先选择" in c.post("/api/job", {"kind": "receive", "paths": []})["error"]
    j = c.job("receive", paths=[str(folder)], date=when, av=False)
    assert j["status"] == "done" and j["title"] == "收图：1020补充（文件夹）", j
    assert any("（文件夹）：收进 4 个文件" in ln for ln in j["lines"])
    j = c.job("receive", paths=[str(p) for p, _ in sample_zips[:2]], date="2026-10-21", av=False)
    assert j["status"] == "done" and j["title"].endswith("等 2 个文件"), j
    assert [b["archive"] for b in c.get("/api/state")["batches"]] == ["1020补充（文件夹）", "示例综合体投标图纸.zip、示例综合体投标图纸0802.zip"]


def test_one_job_at_a_time(running, demo_copy, monkeypatch):
    c = running(demo_copy)
    gate = threading.Event()
    monkeypatch.setattr(server.ops, "build", lambda proj, as_of=None, log=print: gate.wait(5) and {})
    assert c.post("/api/job", {"kind": "build"})["status"] == "running"
    assert "还在进行" in c.post("/api/job", {"kind": "build"})["error"]
    assert "还在进行" in c.post("/api/app/close")["error"]
    gate.set()
    for _ in range(100):
        if c.get("/api/job")["status"] != "running":
            break
        time.sleep(0.02)
    assert c.get("/api/job")["status"] == "done"


def test_settings_never_echo_the_key(running, demo_copy, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = running(demo_copy)
    est = c.job("estimate", scope="all", tier="economy")
    assert est["status"] == "done" and est["result"]["pages"] > 0
    r = c.post("/api/job", {"kind": "recognize", "executor": "claude", "task": est["result"]["task"]})
    assert "API 密钥" in r["error"]
    info = c.post("/api/app/settings", {"api_key": "sk-ant-api03-abcdefghijklmnop"})
    assert info["settings"]["has_api_key"] and "abcdefghijklmnop" not in json.dumps(info)
    assert info["claude_ready"]
    assert "不存在" in c.post("/api/job", {"kind": "recognize", "executor": "claude", "task": str(demo_copy.root)})["error"]
    c.post("/api/app/settings", {"api_key": ""})
    assert not c.get("/api/app")["settings"]["has_api_key"]


# ---------------- 安装版更新 ----------------

def _fake_github(monkeypatch, builds, payload=b"installer-bytes", bad_sha=False, broken=False):
    sha = hashlib.sha256(b"tampered" if bad_sha else payload).hexdigest()
    rels = [{"tag_name": f"b{n}", "name": f"0.2.0 构建 {n}", "published_at": f"2026-10-{n:02d}T00:00:00Z", "draft": False,
             "prerelease": False, "body": f"- 功能 {n}\n- 修复 {n}\n\n<!-- commit: abc{n} -->",
             "assets": [{"name": f"bidbench-setup-b{n}.exe", "size": len(payload),
                         "browser_download_url": f"https://github.com/x/releases/download/b{n}/bidbench-setup-b{n}.exe"},
                        {"name": f"bidbench-setup-b{n}.exe.sha256",
                         "browser_download_url": f"https://github.com/x/releases/download/b{n}/bidbench-setup-b{n}.exe.sha256"}]}
            for n in builds]
    rels.append({"tag_name": "nightly", "assets": [], "draft": False, "prerelease": False})
    seen = []

    class Resp(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Broken(Resp):  # 下载到一半断网
        def read(self, *a):
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")

    def fake_open(url, timeout=30):
        seen.append(url)
        if "api.github.com" in url:
            return Resp(json.dumps(rels).encode())
        if url.endswith(".sha256"):
            return Resp(f"{sha}  x.exe\n".encode())
        return Broken(payload) if broken else Resp(payload)

    monkeypatch.setattr(release, "_open", fake_open)
    return seen


def test_release_check_apply_rollback(monkeypatch):
    monkeypatch.setenv("BIDBENCH_BUILD_OVERRIDE", "5")
    _fake_github(monkeypatch, [7, 6, 5, 3])
    st = release.check()
    assert st["status"] == "available" and st["latest"] == "b7" and st["previous"] == "b3"
    assert [c["title"] for c in st["commits"]] == ["功能 7", "修复 7", "功能 6", "修复 6"]
    r = release.apply()
    assert r["updated_to"] == "b7" and open(r["installer"], "rb").read() == b"installer-bytes"
    assert release.rollback()["rolled_back_to"] == "b3"
    monkeypatch.setenv("BIDBENCH_BUILD_OVERRIDE", "7")
    assert release.check()["status"] == "up_to_date"


def _downloads():
    return sorted(Path(tempfile.gettempdir()).glob(release.DOWNLOAD_PREFIX + "*"))


def test_release_rejects_bad_checksum_and_uses_mirror(monkeypatch):
    monkeypatch.setenv("BIDBENCH_BUILD_OVERRIDE", "1")
    _fake_github(monkeypatch, [2], bad_sha=True)
    with pytest.raises(release.UpdateError, match="校验不通过"):
        release.apply()
    _fake_github(monkeypatch, [2], broken=True)
    with pytest.raises(release.UpdateError, match="下载中断了"):
        release.apply()
    assert _downloads() == []  # 没成功的下载连临时文件夹一起删掉
    seen = _fake_github(monkeypatch, [2])
    appconfig.update_settings(download_prefix="https://mirror.example/")
    release.apply()
    assert len(_downloads()) == 1
    assert any(u.startswith("https://mirror.example/https://github.com/") for u in seen)
    assert all(u.startswith("https://api.github.com") for u in seen if "api" in u)  # 查询接口不走加速前缀


def test_installed_update_job_launches_installer(running, demo_copy, monkeypatch):
    monkeypatch.setenv("BIDBENCH_RELEASE_MODE", "1")
    monkeypatch.setenv("BIDBENCH_BUILD_OVERRIDE", "1")
    _fake_github(monkeypatch, [2])
    launched = []
    monkeypatch.setattr(release, "launch_installer", launched.append)
    c = running(demo_copy)
    quits = []
    c.httpd.app.quit = lambda delay=0.5: quits.append(delay)
    assert c.get("/api/version")["mode"] == "installer"
    assert c.get("/api/update/check")["status"] == "available"
    assert "后台任务" in c.post("/api/update/apply")["error"]
    j = c.job("update")
    assert j["status"] == "done" and launched and launched[0].endswith("bidbench-setup-b2.exe") and quits


def test_old_update_downloads_are_cleaned_up():
    tmp, now = Path(tempfile.gettempdir()), time.time()

    def make(name, dir_age, file_age=None):
        d = tmp / name
        d.mkdir()
        f = d / "bidbench-setup-b3.exe"
        f.write_bytes(b"x" * 1024)
        os.utime(f, (now - (dir_age if file_age is None else file_age),) * 2)
        os.utime(d, (now - dir_age,) * 2)  # 先写文件再改文件夹时间，写文件会刷新文件夹时间
        return d

    old = make("bidbench-update-old", 2 * 3600)
    busy = make("bidbench-update-busy", 2 * 3600, file_age=60)  # 文件夹早就建了、文件还在写：正在下载
    fresh = make("bidbench-update-new", 60)  # 刚装完，安装程序可能还没退出
    other = make("someone-else", 2 * 3600)
    assert release.cleanup_downloads() == 1
    assert not old.exists() and busy.exists() and fresh.exists() and other.exists()
    assert release.cleanup_downloads() == 0


def test_desktop_smoke(demo_copy, capsys, monkeypatch):
    cleaned = threading.Event()
    monkeypatch.setattr(release, "cleanup_downloads", cleaned.set)
    assert desktop.main(["--smoke", str(demo_copy.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["installed"] is False
    assert cleaned.wait(5)  # 启动时在后台清掉以前的更新下载
