"""本地工作台服务：界面 + JSON 接口。只监听本机，图纸不出电脑。

浏览器（bidbench serve）和桌面窗口（bidbench-desktop）共用这一个服务。
安全：所有改动类请求都要带启动时生成的令牌（只写在本服务返回的页面里），并检查 Host 头，
防止其他网页借浏览器冒充你调用本地接口。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import antivirus, appconfig, export, ops, recognition, release, state, updater
from .store import Project, SchemaTooNew, add_decision

DECISION_KINDS = {"sheet_pick", "sheet_current", "sheet_void", "missing", "fact", "risk"}
TOKEN_META = '<meta name="bidbench-token" content="">'


def _updater():
    """安装版从发布仓库下载安装包更新；git clone 安装的用 git 更新。"""
    return release if release.is_installed() else updater


class Job:
    """后台任务：收图、识别、导出等。一次只跑一个，界面轮询进度。"""

    def __init__(self, kind: str, title: str):
        self.id = secrets.token_hex(4)
        self.kind, self.title = kind, title
        self.status = "running"
        self.lines: list[str] = []
        self.progress: tuple[int, int] | None = None
        self.result = None
        self.error: str | None = None
        self.started = datetime.now().isoformat(timespec="seconds")
        self.finished: str | None = None

    def log(self, msg: str) -> None:
        self.lines.extend(str(msg).splitlines() or [""])
        del self.lines[:-400]

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "lines": self.lines,
                "progress": self.progress, "result": self.result, "error": self.error,
                "started": self.started, "finished": self.finished}


class _App:
    def __init__(self, proj: Project | None, mode: str = "browser"):
        self.proj = proj
        self.mode = mode  # browser / desktop
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.cache: dict[int | None, dict] = {}
        self.httpd = None
        self.update_cache: tuple[float, dict] | None = None
        self.job: Job | None = None
        self.on_quit: list = []  # 桌面窗口注册的关闭动作

    # ---- 更新 ----
    def check_update(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self.update_cache and now - self.update_cache[0] < 600:
            return self.update_cache[1]
        r = _updater().check()
        self.update_cache = (now, r)
        return r

    def restart_after(self, pip_command) -> None:
        """git 安装：响应发出后退出本进程；由独立的小进程补装依赖、再用新代码重启。"""
        root = updater.repo_root()
        log = (root / ".git" / "bidbench_update.log") if root else appconfig.config_dir() / "update.log"
        updater.spawn_helper(os.getpid(), pip_command, updater.restart_command(), log)
        self.quit(0.5)

    def quit(self, delay: float = 0.5) -> None:
        def go():
            for fn in self.on_quit:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - 窗口已经关了等
                    pass
            self.httpd.shutdown()
        threading.Timer(delay, go).start()

    # ---- 项目 ----
    def set_project(self, proj: Project | None) -> None:
        with self.lock:
            self.proj = proj
            self.cache.clear()
        if proj is not None:
            appconfig.add_recent(proj.root, proj.meta["name"])

    def state(self, as_of: int | None = None) -> dict:
        with self.lock:
            if as_of not in self.cache:
                self.cache[as_of] = state.build(self.proj, as_of)
            return self.cache[as_of]

    def invalidate(self) -> None:
        with self.lock:
            self.cache.clear()

    def info(self) -> dict:
        eng = antivirus.find_engine()
        return {"mode": self.mode, "installed": release.is_installed(),
                "project": ({**self.proj.meta, "root": str(self.proj.root)} if self.proj else None),
                "recent": appconfig.recent(), "settings": appconfig.public_settings(),
                "default_dir": str(appconfig.default_projects_dir()), "today": date.today().isoformat(),
                "antivirus": eng.name if eng else None, "claude_ready": ops.has_claude_credentials(),
                "job": self.job.as_dict() if self.job else None}

    # ---- 后台任务 ----
    def start_job(self, kind: str, title: str, fn) -> Job:
        if self.job and self.job.status == "running":
            raise RuntimeError(f"「{self.job.title}」还在进行，等它完成后再操作。")
        job = Job(kind, title)
        self.job = job

        def run():
            try:
                job.result = fn(job)
                job.status = "done"
            except (ops.OpError, updater.UpdateError, ValueError, FileNotFoundError, FileExistsError, SchemaTooNew) as e:
                job.error, job.status = str(e), "error"
                job.log(f"错误：{e}")
            except Exception as e:  # noqa: BLE001 - 意外错误也要告诉用户，并留下记录
                job.error, job.status = f"出错了：{type(e).__name__}: {e}", "error"
                job.log(job.error)
                (appconfig.config_dir() / "error.log").open("a", encoding="utf-8").write(
                    f"\n[{datetime.now():%F %T}] {kind}\n{traceback.format_exc()}")
            finally:
                job.finished = datetime.now().isoformat(timespec="seconds")
                self.invalidate()

        threading.Thread(target=run, daemon=True).start()
        return job


def _job_fn(app: _App, kind: str, body: dict):
    """把界面上的操作翻译成后台任务。返回 (标题, 函数)。"""
    proj = app.proj

    def need_proj():
        if proj is None:
            raise ValueError("请先打开一个项目")
        return proj

    if kind == "receive":
        raw = body.get("paths") if isinstance(body.get("paths"), list) else [body.get("path")]
        paths = [Path(str(p).strip()).expanduser() for p in raw if isinstance(p, str) and p.strip()]
        if not paths:
            raise ValueError("请先选择压缩包、文件或文件夹")
        for p in paths:
            if not p.exists():
                raise ValueError(f"找不到 {p}，请重新选择")
        if len(paths) > 1 and any(p.is_dir() for p in paths):
            raise ValueError("一次只能收一个文件夹；多个文件可以一起收")
        when = str(body.get("date") or date.today().isoformat())
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", when):
            raise ValueError("收图日期格式应为 YYYY-MM-DD")
        pw = [p for p in (body.get("passwords") or []) if isinstance(p, str) and p]

        def fn(job):
            def progress(n, total):
                job.progress = (n, total)
            r = ops.receive(need_proj(), paths, when, body.get("name") or None, pw, body.get("note") or None,
                            av=bool(body.get("av", True)), log=job.log, progress=progress)
            job.progress = None
            ops.build(proj, log=job.log)
            return r
        what = paths[0].name + ("（文件夹）" if paths[0].is_dir() else "") + (f" 等 {len(paths)} 个文件" if len(paths) > 1 else "")
        return f"收图：{what}", fn

    if kind == "estimate":
        scope, tier = body.get("scope") or "new", body.get("tier") or "economy"
        if scope not in recognition.SCOPES or tier not in ("economy", "strong"):
            raise ValueError("识别范围或档位不对")

        def fn(job):
            job.log("正在生成识别任务包（渲染页面图片）……")
            est = ops.prepare_claude(need_proj(), scope, tier)
            if est is None:
                job.log(f"范围「{recognition.SCOPES[scope]}」内没有需要识别的页面。")
                return None
            job.log(ops.estimate_text(est))
            return {**est, "tier": tier, "scope": scope}
        return "估算识别花费", fn

    if kind == "recognize":
        executor = body.get("executor")
        if executor == "simulated":
            def fn(job):
                r = ops.recognize_simulated(need_proj(), log=job.log)
                ops.build(proj, log=job.log)
                return r
            return "模拟识别", fn
        if executor != "claude":
            raise ValueError("未知的识别方式")
        task = Path(str(body.get("task") or ""))
        tasks_root = need_proj().path("recognition", "tasks").resolve()
        if not task.is_dir() or tasks_root not in task.resolve().parents:
            raise ValueError("任务包不存在，请先估算花费")
        tier = body.get("tier") if body.get("tier") in ("economy", "strong") else "economy"
        if not ops.has_claude_credentials():
            raise ValueError("还没有设置 Claude API 密钥，请先到“设置”里填写")

        def fn(job):
            def progress(n, total):
                job.progress = (n, total)
            r = ops.recognize_claude(proj, task, tier, log=job.log, progress=progress)
            ops.build(proj, log=job.log)
            return r
        return "Claude 识别", fn

    if kind == "build":
        return "重新计算报告", lambda job: ops.build(need_proj(), log=job.log)

    if kind == "export":
        what = body.get("what")
        if what not in ("current", "mirror"):
            raise ValueError("导出类型不对")
        return ("导出现行图纸" if what == "current" else "导出数据镜像"), \
            lambda job: {"path": str(ops.export_(need_proj(), what, log=job.log))}

    if kind == "scan":
        return "文件安全扫描", lambda job: ops.scan(need_proj(), av=bool(body.get("av", True)), log=job.log)

    if kind in ("update", "rollback"):
        if not release.is_installed():
            raise ValueError("只有安装版用这种方式更新")

        def fn(job):
            def progress(done, total):
                job.progress = (done, total)
            job.log("正在下载安装包……")
            r = release.apply(progress) if kind == "update" else release.rollback(progress)
            job.log("下载完成，校验通过。正在安装，工作台会自动关闭并重新打开。")
            release.launch_installer(r["installer"])
            app.quit(1.5)
            return r
        return ("更新" if kind == "update" else "回退到上一版"), fn

    raise ValueError("未知的操作")


def _open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 - 只打开文件夹
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def make_handler(app: _App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静一点
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            host = self.server.server_address[0]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"{host}:{port}"}
            return (self.headers.get("Host") or "") in allowed

        def _need_project(self) -> bool:
            if app.proj is None:
                self._json({"error": "还没有打开项目", "no_project": True}, 409)
                return False
            return True

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._json({"error": "拒绝访问"}, 403)
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                html = resources.files("bidbench").joinpath("web/index.html").read_text(encoding="utf-8")
                html = html.replace(TOKEN_META, f'<meta name="bidbench-token" content="{app.token}">')
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/version":
                return self._json(_updater().version_info())
            if u.path == "/api/update/check":
                return self._json(app.check_update(force="force" in parse_qs(u.query)))
            if u.path == "/api/app":
                return self._json(app.info())
            if u.path == "/api/job":
                return self._json(app.job.as_dict() if app.job else None)
            if u.path == "/api/state":
                if not self._need_project():
                    return None
                q = parse_qs(u.query)
                as_of = int(q["as_of"][0]) if q.get("as_of") else None
                return self._json(app.state(as_of))
            m = re.match(r"^/api/page/([0-9a-f]{12}-p\d{3})\.pdf$", u.path)
            if m:
                if not self._need_project():
                    return None
                try:
                    data = export.page_pdf_bytes(app.proj, m.group(1))
                except (TypeError, KeyError):
                    return self._json({"error": "没有这个页面"}, 404)
                return self._send(200, data, "application/pdf")
            return self._json({"error": "未找到"}, 404)

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._json({"error": "拒绝访问"}, 403)
            if not secrets.compare_digest(self.headers.get("X-Bidbench-Token") or "", app.token):
                return self._json({"error": "页面已过期，请刷新后重试"}, 403)
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                return self._json({"error": "请求过大"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求不是合法 JSON"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "请求格式不对"}, 400)
            try:
                return self._post(u.path, body)
            except (ValueError, FileNotFoundError, FileExistsError, SchemaTooNew, RuntimeError) as e:
                return self._json({"error": str(e)}, 400)

        def _post(self, path: str, body: dict):
            if path.startswith("/api/app/") or path == "/api/job":
                return self._post_app(path, body)
            if path in ("/api/update/apply", "/api/update/rollback"):
                if release.is_installed():
                    return self._json({"error": "安装版请用后台任务更新"}, 400)
                try:
                    r = updater.apply() if path.endswith("apply") else updater.rollback()
                except updater.UpdateError as e:
                    return self._json({"error": str(e)}, 400)
                app.update_cache = None
                app.restart_after(r["pip_command"])  # 只是安排在 0.5 秒后退出，响应照常发出
                return self._json({**r, "restarting": True})
            if not self._need_project():
                return None
            if path == "/api/decision":
                kind, key, value = body.get("kind"), body.get("key"), body.get("value")
                if kind not in DECISION_KINDS or not isinstance(key, str) or not isinstance(value, str):
                    return self._json({"error": "决定的格式不对"}, 400)
                add_decision(app.proj, kind, key, value, body.get("note"))
                app.invalidate()
                return self._json(app.state())
            if path == "/api/proposal":
                from .assistant import Toolkit, ToolError
                pid, action = body.get("id"), body.get("action")
                if not isinstance(pid, int) or action not in ("confirm", "reject"):
                    return self._json({"error": "提议操作的格式不对"}, 400)
                kit = Toolkit(app.proj, source="工作台")
                try:
                    r = kit.confirm(pid) if action == "confirm" else kit.reject(pid, body.get("note"))
                except ToolError as e:
                    return self._json({"error": str(e)}, 400)
                app.invalidate()
                st = dict(app.state())
                st["last_outcome"] = r["outcome"]
                return self._json(st)
            if path == "/api/baseline":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json({"error": "需要基线名称"}, 400)
                state.create_baseline(app.proj, name, body.get("sheets") or None, body.get("note"))
                app.invalidate()
                return self._json(app.state())
            return self._json({"error": "未找到"}, HTTPStatus.NOT_FOUND)

        def _post_app(self, path: str, body: dict):
            busy = app.job and app.job.status == "running"
            if path == "/api/job":
                title, fn = _job_fn(app, str(body.get("kind")), body)
                return self._json(app.start_job(str(body.get("kind")), title, fn).as_dict())
            if path == "/api/app/settings":
                appconfig.update_settings(body.get("api_key"), body.get("download_prefix"))
                return self._json(app.info())
            if path == "/api/app/open_folder":
                p = Path(str(body.get("path") or ""))
                if not p.is_dir():
                    raise ValueError("文件夹不存在")
                _open_folder(p)
                return self._json({"ok": True})
            if busy:
                raise RuntimeError(f"「{app.job.title}」还在进行，等它完成后再切换项目。")
            if path == "/api/app/open":
                root = Path(str(body.get("path") or "")).expanduser()
                if not (root / "bidbench.json").exists():
                    raise ValueError("这个文件夹不是 bidbench 项目（里面没有 bidbench.json）")
                proj = Project.open(root)
                proj.connect().close()  # 需要升级数据库时现在就升级，版本过新时现在就报错
                app.set_project(proj)
                return self._json(app.info())
            if path == "/api/app/new":
                root = Path(str(body.get("path") or "")).expanduser()
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("请填写项目名称")
                if root.exists() and any(root.iterdir()):
                    raise ValueError("这个文件夹里已经有东西了，请选一个空文件夹或新文件夹")
                app.set_project(Project.init(root, name))
                return self._json(app.info())
            if path == "/api/app/close":
                app.set_project(None)
                return self._json(app.info())
            if path == "/api/app/forget":
                appconfig.forget(str(body.get("path") or ""))
                return self._json(app.info())
            return self._json({"error": "未找到"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(proj: Project | None, host: str = "127.0.0.1", port: int = 8765, mode: str = "browser") -> ThreadingHTTPServer:
    app = _App(proj, mode)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    app.httpd = httpd
    httpd.app = app
    if proj is not None:
        appconfig.add_recent(proj.root, proj.meta["name"])
    return httpd
