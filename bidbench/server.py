"""本地工作台服务：浏览器界面 + JSON 接口。只监听本机，图纸不出电脑。"""

from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import export, state
from .store import Project, add_decision

DECISION_KINDS = {"sheet_pick", "sheet_current", "sheet_void", "missing", "fact"}


class _App:
    def __init__(self, proj: Project):
        self.proj = proj
        self.lock = threading.Lock()
        self.cache: dict[int | None, dict] = {}

    def state(self, as_of: int | None = None) -> dict:
        with self.lock:
            if as_of not in self.cache:
                self.cache[as_of] = state.build(self.proj, as_of)
            return self.cache[as_of]

    def invalidate(self) -> None:
        with self.lock:
            self.cache.clear()


def make_handler(app: _App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静一点
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                html = resources.files("bidbench").joinpath("web/index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/api/state":
                q = parse_qs(u.query)
                as_of = int(q["as_of"][0]) if q.get("as_of") else None
                return self._json(app.state(as_of))
            m = re.match(r"^/api/page/([0-9a-f]{12}-p\d{3})\.pdf$", u.path)
            if m:
                try:
                    data = export.page_pdf_bytes(app.proj, m.group(1))
                except (TypeError, KeyError):
                    return self._json({"error": "没有这个页面"}, 404)
                return self._send(200, data, "application/pdf")
            return self._json({"error": "未找到"}, 404)

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                return self._json({"error": "请求过大"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求不是合法 JSON"}, 400)
            if u.path == "/api/decision":
                kind, key, value = body.get("kind"), body.get("key"), body.get("value")
                if kind not in DECISION_KINDS or not isinstance(key, str) or not isinstance(value, str):
                    return self._json({"error": "决定的格式不对"}, 400)
                add_decision(app.proj, kind, key, value, body.get("note"))
                app.invalidate()
                return self._json(app.state())
            if u.path == "/api/baseline":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json({"error": "需要基线名称"}, 400)
                try:
                    state.create_baseline(app.proj, name, body.get("sheets") or None, body.get("note"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                app.invalidate()
                return self._json(app.state())
            return self._json({"error": "未找到"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(proj: Project, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    app = _App(proj)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return httpd
