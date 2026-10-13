"""本地工作台服务：界面 + JSON 接口。只监听本机，图纸不出电脑。

浏览器（bidbench serve）和桌面窗口（bidbench-desktop）共用这一个服务。
安全：所有改动类请求都要带启动时生成的令牌（只写在本服务返回的页面里），并检查 Host 头，
防止其他网页借浏览器冒充你调用本地接口。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import antivirus, appconfig, chat, debuglog, export, oplog, ops, recognition, release, state, updater
from .store import Project, SchemaTooNew, add_decision, now_iso

DECISION_KINDS = {"sheet_pick", "sheet_current", "sheet_void", "missing", "fact", "risk", "catalog_rev", "name_check",
                  "file_tower", "tower_alias"}
PAGE_ID = re.compile(r"[0-9a-f]{12}-p\d{3}")
TOKEN_META = '<meta name="bidbench-token" content="">'
PROJECT_JOBS = {"receive", "estimate", "recognize", "classify", "build", "export", "scan"}  # 记进项目操作日志的任务
SOURCE = "工作台"

log = logging.getLogger(__name__)


def _updater():
    """安装版从发布仓库下载安装包更新；git clone 安装的用 git 更新。"""
    return release if release.is_installed() else updater


class Job:
    """后台任务：收图、识别、导出等。一次只跑一个，界面轮询进度。"""

    def __init__(self, kind: str, title: str, proj: Project | None = None):
        self.id = secrets.token_hex(4)
        self.kind, self.title, self.proj = kind, title, proj
        self.status = "running"
        self.lines: list[str] = []  # 全部输出；界面只取最后 400 行，操作日志里留头尾
        self.progress: tuple[int, int] | None = None
        self.stage: str | None = None      # 分几步的任务现在在哪一步，如“准备页面图片”“识别”
        self.imported: int | None = None   # 识别途中已导入的页数
        self.result = None
        self.error: str | None = None
        self.started = datetime.now().isoformat(timespec="seconds")
        self.started_at, self.t0 = now_iso(), time.monotonic()
        self.finished: str | None = None
        self.logged = False
        self.lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self.lock:
            self.lines.extend(str(msg).splitlines() or [""])

    def write_oplog(self, status: str) -> None:
        """记进项目的操作日志，只记一次（关窗口时记“中断”，之后任务线程即使跑完也不再记）。"""
        with self.lock:
            if self.logged or self.proj is None or self.kind not in PROJECT_JOBS:
                return
            self.logged = True
            lines = list(self.lines)
        oplog.write(self.proj, self.kind, self.title, source=SOURCE, status=status, lines=lines, error=self.error,
                    started=self.started_at, seconds=time.monotonic() - self.t0, result=self.result)

    def as_dict(self) -> dict:
        with self.lock:
            lines = self.lines[-400:]
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "lines": lines,
                "progress": self.progress, "stage": self.stage, "imported": self.imported,
                "result": self.result, "error": self.error,
                "started": self.started, "finished": self.finished}


class _App:
    def __init__(self, proj: Project | None, mode: str = "browser"):
        self.proj = proj
        self.mode = mode  # browser / desktop
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.cache: dict[int | None, dict] = {}
        self.db_version: str | None = None  # 缓存对应的数据库修改计数
        self.httpd = None
        self.update_cache: tuple[float, dict] | None = None
        self.job: Job | None = None
        self.chat = chat.Chat(proj) if proj else None  # 工作台的 AI 助手（Codex）
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
            self.close_down()
            for fn in self.on_quit:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - 窗口已经关了等
                    pass
            self.httpd.shutdown()
        threading.Timer(delay, go).start()

    # ---- 项目 ----
    def set_project(self, proj: Project | None) -> None:
        if self.chat:
            self.chat.stop()
        with self.lock:
            self.proj = proj
            self.cache.clear()
            self.chat = chat.Chat(proj) if proj else None
        log.info("打开项目：%s", proj.root if proj else "（无，回到项目选择）")
        if proj is not None:
            appconfig.add_recent(proj.root, proj.meta["name"])

    def db_version_now(self) -> str | None:
        """数据库文件头里的修改计数（第 24~27 字节）。外部 AI 助手、命令行改了项目，这个数都会变。"""
        if not self.proj:
            return None
        try:
            with open(self.proj.db_path, "rb") as f:
                f.seek(24)
                return f.read(4).hex() or None
        except OSError:
            return None

    def state(self, as_of: int | None = None) -> dict:
        with self.lock:
            v = self.db_version_now()
            if v != self.db_version:  # 别的程序改过项目：缓存作废
                self.cache.clear()
                self.db_version = v
            if as_of not in self.cache:
                self.cache[as_of] = {**state.build(self.proj, as_of), "db_version": v}
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
                "log_dir": str(debuglog.folder()), "job": self.job.as_dict() if self.job else None}

    # ---- 后台任务 ----
    def start_job(self, kind: str, title: str, fn) -> Job:
        if self.job and self.job.status == "running":
            raise RuntimeError(f"「{self.job.title}」还在进行，等它完成后再操作。")
        job = Job(kind, title, self.proj)
        self.job = job
        log.info("任务开始：%s（%s）", title, kind)

        def run():
            status = "error"
            try:
                job.result = fn(job)
                status = "done"
            except (ops.OpError, updater.UpdateError, ValueError, FileNotFoundError, FileExistsError, SchemaTooNew) as e:
                job.error = str(e)
                job.log(f"错误：{e}")
                log.info("任务出错：%s：%s", title, e)
            except Exception as e:  # noqa: BLE001 - 意外错误也要告诉用户，并在排错日志里留下完整记录
                job.error = f"出错了：{type(e).__name__}: {e}。详细信息已写进排错日志（设置 → 打开排错日志文件夹）"
                job.log(job.error)
                log.exception("任务意外出错：%s（%s）", title, kind)
            finally:
                job.finished = datetime.now().isoformat(timespec="seconds")
                self.invalidate()
                log.info("任务结束：%s，%s，用时 %.1f 秒", title, status, time.monotonic() - job.t0)
                try:
                    job.write_oplog(status)
                except Exception:  # noqa: BLE001 - 记日志出错不能影响任务本身
                    log.exception("写操作日志出错：%s", title)
                job.status = status  # 最后才标完成：界面（和测试）看到完成时，操作日志已经写好

        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return job

    def close_down(self) -> None:
        """退出前：还在跑的任务记一笔“中断”，还在回答的 AI 助手停下来。"""
        self.abandon_job()
        if self.chat:
            self.chat.stop()

    def abandon_job(self) -> None:
        """退出时还有任务在跑（关窗口时用户确认了退出）：操作日志里记一笔“中断”。"""
        job = self.job
        if job and job.status == "running":
            log.warning("退出时任务还没完成：%s", job.title)
            job.write_oplog("interrupted")


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
            job.stage = "智能分类"
            ops.classify_if_ready(proj, log=job.log)  # 用过 Codex、Claude 识别的：新图纸先按名字分一下类
            return r
        return oplog.receive_title(paths), fn

    if kind == "estimate":
        scope, tier = body.get("scope") or "new", body.get("tier") or "economy"
        executor, model = body.get("executor") or "claude", _model(body)
        if scope not in recognition.SCOPES or tier not in ("economy", "strong") or executor not in EXECUTORS:
            raise ValueError("识别范围、档位或识别方式不对")
        appconfig.remember("recognize", {"executor": executor, "scope": scope, "tier": tier, "model": model})

        def fn(job):
            est = ops.plan_recognition(need_proj(), scope, executor, tier, model)  # 只估算，不渲染图片
            if est is None:
                job.log(f"范围「{recognition.SCOPES[scope]}」内没有需要识别的页面。")
                return None
            job.log(ops.estimate_text(est))
            return {**est, "tier": tier, "scope": scope, "executor": executor}
        return f"{oplog.TITLES['estimate']}（{EXECUTORS[executor]} {'经济档' if tier == 'economy' else '强档'}）", fn

    if kind == "recognize":
        executor = body.get("executor")
        if executor == "simulated":
            def fn(job):
                r = ops.recognize_simulated(need_proj(), log=job.log)
                ops.build(proj, log=job.log)
                return r
            return oplog.TITLES["simulated"], fn
        if executor not in EXECUTORS:
            raise ValueError("未知的识别方式")
        task = None
        if body.get("task"):  # 续跑已有的任务包
            task = Path(str(body["task"]))
            tasks_root = need_proj().path("recognition", "tasks").resolve()
            if not task.is_dir() or tasks_root not in task.resolve().parents:
                raise ValueError("任务包不存在，请先估算")
        scope = body.get("scope") or "new"
        if scope not in recognition.SCOPES:
            raise ValueError("识别范围不对")
        priorities = body.get("priorities")
        if priorities is not None and (not isinstance(priorities, list) or not priorities
                                       or any(k not in recognition.PRIORITY_RANK for k in priorities)):
            raise ValueError("请至少选一档要识别的图纸")
        pages = body.get("pages")  # 按需识别：图谱里点的那张图、那一框
        if pages is not None and (not isinstance(pages, list) or not pages
                                  or any(not isinstance(x, str) or not PAGE_ID.fullmatch(x) for x in pages)):
            raise ValueError("要识别的页面不对")
        tier = body.get("tier") if body.get("tier") in ("economy", "strong") else "economy"
        model = _model(body)
        if executor == "claude" and not ops.has_claude_credentials():
            raise ValueError("还没有设置 Claude API 密钥，请先到“设置”里填写")
        if executor == "codex":
            st = ops.codex_status()
            if not st["ready"]:
                raise ValueError(st["hint"] or "Codex 还不能用")

        def fn(job):
            def progress(n, total):
                job.progress = (n, total)
            along = ops.ClassifyAlong(proj, executor, model, job.log, progress)  # 边识别边分类

            def imported(n):
                job.imported = n
                along(n)
            t = task
            if t is None:  # 按选中的几档准备页面图片：先目录、说明、平面，详图大样放最后
                job.stage = "准备页面图片"
                job.log("正在准备页面图片……")
                t = recognition.make_task(need_proj(), scope=scope, priorities=priorities, progress=progress, pages=pages)
                if t is None:
                    job.log("没有需要识别的页面。")
                    return None
            job.stage, job.progress = "识别", None
            recognize = ops.recognize_codex if executor == "codex" else ops.recognize_claude
            try:
                r = recognize(proj, t, tier, model, log=job.log, progress=progress, on_import=imported)
            finally:
                along.wait()
            job.stage, job.progress = "智能分类", None  # 把新出现的图纸和事实分完；分不成不影响识别结果
            along.finish()
            job.stage, job.progress = "计算台账", None
            ops.build(proj, log=job.log)
            return r
        return f"{oplog.TITLES[executor]}（{'经济档' if tier == 'economy' else '强档'}）", fn

    if kind == "classify":
        executor = body.get("executor") or "codex"
        if executor not in (*EXECUTORS, "simulated"):
            raise ValueError("未知的分类方式")
        model = _model(body)
        if executor == "claude" and not ops.has_claude_credentials():
            raise ValueError("还没有设置 Claude API 密钥，请先到“设置”里填写")
        if executor == "codex":
            st = ops.codex_status()
            if not st["ready"]:
                raise ValueError(st["hint"] or "Codex 还不能用")
        redo = bool(body.get("redo"))

        def fn(job):
            def progress(n, total):
                job.progress = (n, total)
            job.stage = "智能分类"
            r = ops.classify(need_proj(), executor, model, redo, log=job.log, progress=progress)
            job.stage, job.progress = "计算台账", None
            ops.build(proj, log=job.log)
            return r
        return f"{oplog.TITLES['classify']}（{ops.CLASSIFY_BY[executor]}{'，全部重新分' if redo else ''}）", fn

    if kind == "build":
        return oplog.TITLES["build"], lambda job: ops.build(need_proj(), log=job.log)

    if kind == "export":
        what = body.get("what")
        if what not in ("current", "mirror"):
            raise ValueError("导出类型不对")
        return oplog.TITLES[what], lambda job: {"path": str(ops.export_(need_proj(), what, log=job.log))}

    if kind == "scan":
        return oplog.TITLES["scan"], lambda job: ops.scan(need_proj(), av=bool(body.get("av", True)), log=job.log)

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


EXECUTORS = {"claude": "Claude", "codex": "Codex"}


def _model(body: dict) -> str | None:
    """界面选的模型；不选就按档位默认。只接受模型名里会出现的字符，防止拼进命令行的参数被当成选项。"""
    m = body.get("model")
    if not m:
        return None
    if not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", m):
        raise ValueError("模型名称不对")
    return m


def _recognizers(refresh: bool = False) -> dict:
    """识别页面用：两种识别方式能不能用、有哪些模型。"""
    from .executors import claude
    return {"claude": {"ready": ops.has_claude_credentials(), "models": claude.model_list(),
                       "defaults": {t: c["model"] for t, c in claude.TIERS.items()}},
            "codex": ops.codex_status(refresh), "prefs": appconfig.prefs().get("recognize") or {}}


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
            if u.path == "/api/chat":
                if not self._need_project():
                    return None
                st = app.chat.state()
                if "status" in parse_qs(u.query):  # 打开面板时才查 Codex（要调用几次命令行）
                    from .assistant import list_suggestions
                    st.update(codex=ops.codex_status(refresh="refresh" in parse_qs(u.query)),
                              prefs=appconfig.prefs().get("assistant") or {}, suggestions=len(list_suggestions()))
                return self._json(st)
            if u.path == "/api/recognizers":
                return self._json(_recognizers(refresh="refresh" in parse_qs(u.query)))
            if u.path == "/api/log":
                if not self._need_project():
                    return None
                q = parse_qs(u.query)
                limit = int(q["limit"][0]) if q.get("limit", [""])[0].isdigit() else 200
                return self._json({"entries": oplog.read(app.proj, min(limit, 2000)),
                                   "folder": str(oplog.folder(app.proj)), "debug_folder": str(debuglog.folder())})
            if u.path == "/api/state/version":  # 界面定时看一眼，变了再取整个状态
                job = app.job
                running = bool(job and job.status == "running")
                return self._json({"version": app.db_version_now(), "busy": running, "job": job.kind if running else None})
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
                log.info("请求没有完成：%s：%s", u.path, e)
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
            if path == "/api/chat":
                model = _model(body)
                st = ops.codex_status()
                if not st["ready"]:
                    raise ValueError(st["hint"] or "Codex 还不能用")
                app.chat.ask(str(body.get("question") or ""), model)
                appconfig.remember("assistant", {"model": model})
                return self._json(app.chat.state())
            if path == "/api/chat/stop":
                app.chat.stop()
                return self._json(app.chat.state())
            if path == "/api/chat/new":
                app.chat.reset()
                return self._json(app.chat.state())
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
                changed = [k for k in ("api_key", "download_prefix") if body.get(k) is not None]
                log.info("修改设置：%s", "、".join(changed) or "无")  # 只记改了哪几项，不记内容
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
                proj = Project.init(root, name)
                oplog.write(proj, "init", f"新建项目「{name}」", source=SOURCE)
                app.set_project(proj)
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
