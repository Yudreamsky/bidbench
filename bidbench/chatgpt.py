"""用 ChatGPT 账号登录，直接调用 OpenAI 模型（OpenAI 的“Sign in with ChatGPT”，开源本地软件用套餐额度）。

登录后识别、分类直接发到 OpenAI 的 Responses 接口（https://api.openai.com/v1/responses），占用 ChatGPT 套餐的额度，
不另外计费，也不用先装 Codex 命令行。没登录时照旧用本机的 Codex 命令行（executors/codex.py 里自动选）。

流程（https://developers.openai.com/siwc/token-sharing-open-source）：
1. 本机先起一个只听 127.0.0.1 的回调服务，再在浏览器里打开 OpenAI 的授权页（OAuth + PKCE）；
2. 用户在浏览器里登录、同意后，浏览器带着 code 回到本机，这里用 code 换令牌，核对 state、nonce、签发方和对象；
3. 令牌存在本机用户设置目录的 chatgpt.json（只有本人能读），一小时过期，快到期时用刷新令牌换新的；
4. 退出登录先请 OpenAI 吊销刷新令牌，再删本机的令牌；登记号（client_id）和这台电脑的编号留着，下次登录接着用。

安全：令牌只存在这个文件里，不写日志、不放进网址、不发给 OpenAI 以外的地方。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from . import appconfig

log = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
API = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
REGISTER_CLIENT = "dynamic_agent_client"  # 第一次登录用它，OpenAI 回调时发一个本机专用的登记号
APP_NAME = "bidbench"
CALLBACK_PATH = "/auth/callback"
PORTS = (1456, 0)              # 先试固定端口，被占了就随便找一个空的（只有端口可以变）
LOGIN_TIMEOUT = 600            # 浏览器里十分钟没登录完就算了
REFRESH_BEFORE = 300           # 离过期不到 5 分钟就先刷新
HTTP_TIMEOUT = 60
STREAM_TIMEOUT = 900           # 一页最多等 15 分钟（和 Codex 命令行一样）
RETRY_WAITS = (5, 20, 60)      # 服务暂时不可用时隔多久重试
USAGE_URL = "https://chatgpt.com/settings/usage"

_lock = threading.RLock()      # 同一个进程里读写令牌、刷新令牌一个一个来
_sleep = time.sleep            # 测试里换掉，不真的等


def _urlopen(req: Request, timeout: float):
    """所有联网都走这里（测试里换成假的）。跟随系统代理设置。"""
    return urlopen(req, timeout=timeout)  # noqa: S310 - 只访问上面写死的 OpenAI 地址


# ---------------- 出错 ----------------

class ChatGPTError(RuntimeError):
    """kind：limit=额度暂时用完或服务暂时不可用，等一会儿能接着做；fatal=整次都会失败（没登录、账号不能用、
    参数不支持）；page=只算这一页失败。message 是给人看的中文。"""

    def __init__(self, message: str, kind: str = "page", code: str = "", status: int = 0):
        super().__init__(message)
        self.kind, self.code, self.status = kind, code, status


NOT_SIGNED_IN = "还没有用 ChatGPT 账号登录"
LIMIT_TEXT = f"ChatGPT 套餐的用量额度暂时用完了（用量见 {USAGE_URL}）"

_CODES = {  # https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery
    "subscription_sharing_user_not_eligible": ("fatal", "这个 ChatGPT 账号（或所在的工作区）不能把套餐额度给本软件用。可以换个账号登录，或改用本机的 Codex 命令行"),
    "subscription_sharing_usage_limit_exceeded": ("limit", LIMIT_TEXT),
    "subscription_sharing_usage_unavailable": ("retry", "ChatGPT 套餐额度暂时查不到"),
    "subscription_sharing_user_unavailable": ("retry", "ChatGPT 账号暂时不可用"),
    "subscription_sharing_unsupported_capability": ("fatal", "ChatGPT 套餐额度不支持这次请求里的某个参数"),
    "subscription_sharing_route_not_supported": ("fatal", "ChatGPT 套餐额度不支持这个接口"),
    "subscription_sharing_invalid_user": ("auth", "ChatGPT 登录已失效"),
    "chatpass_v2_scope_not_authorized": ("fatal", "登录时没有授权使用 ChatGPT 套餐额度，请退出后重新登录并同意"),
    "chatpass_v2_invalid_authorization_context": ("fatal", "登录授权有问题，请退出后重新登录"),
    "model_not_found": ("fatal", "这个 ChatGPT 账号不能用这个模型，请换一个模型"),
}
_REFRESH_DEAD = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                 "refresh_token_invalidated", "refresh_token_reused"}


def _error_of(raw: bytes | str) -> dict:
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if isinstance(d, dict) and isinstance(d.get("error"), dict):
        return d["error"]
    if isinstance(d, dict) and isinstance(d.get("error"), str):  # OAuth 的出错：{"error": "invalid_grant", ...}
        return {"code": d["error"], "message": d.get("error_description") or d["error"]}
    return d if isinstance(d, dict) else {}


def _api_error(status: int, err: dict) -> ChatGPTError:
    code = str(err.get("code") or err.get("type") or "")
    msg = str(err.get("message") or "")
    kind, text = _CODES.get(code, (None, None))
    if kind is None:
        if status == 401:
            kind, text = "auth", "ChatGPT 登录已失效"
        elif status == 403:
            kind, text = "fatal", "OpenAI 拒绝了这次请求（可能是所在地区或账号权限）"
        elif status == 429:
            kind, text = "limit", LIMIT_TEXT
        elif status >= 500:
            kind, text = "retry", "OpenAI 的服务暂时不可用"
        elif "model" in (code + msg).lower() and ("not" in msg.lower() or "unsupported" in msg.lower()):
            kind, text = "fatal", "这个 ChatGPT 账号不能用这个模型，请换一个模型"
        else:
            kind, text = "page", "OpenAI 返回出错"
    if code == "subscription_sharing_unsupported_capability" and err.get("param"):
        text += f"：{err['param']}"
    detail = f"（{code or status}{'：' + msg if msg else ''}）"
    return ChatGPTError(text + detail, kind, code, status)


# ---------------- 本机保存的登录信息 ----------------

def credentials_path():
    return appconfig.config_dir() / "chatgpt.json"


def _load() -> dict:
    try:
        d = json.loads(credentials_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(d: dict) -> None:
    """原子写入，只有本人能读（Unix 上 0600；Windows 的用户设置目录本来就只有本人能读）。"""
    p = credentials_path()
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def host_id() -> str:
    """这台电脑的编号：第一次登录前生成一次，以后一直用它（不是密码，也不含个人信息）。"""
    with _lock:
        d = _load()
        if not d.get("host_id"):
            d["host_id"] = f"urn:uuid:{uuid.uuid4()}"
            _save(d)
        return d["host_id"]


def status() -> dict:
    """给界面看：登没登录、哪个账号、能不能用套餐额度。不联网，不回传令牌。"""
    d = _load()
    signed_in = bool(d.get("refresh_token") or d.get("access_token"))
    plan = signed_in and PLAN_SCOPE in (d.get("scopes") or "").split()
    hint = None
    if signed_in and not plan:
        hint = "已登录，但登录时没有同意使用 ChatGPT 套餐额度：退出后重新登录，在授权页上同意。"
    return {"signed_in": signed_in, "ready": bool(plan), "email": d.get("email") or "", "hint": hint,
            "usage_url": USAGE_URL, "login": _login_state()}


def ready() -> bool:
    return status()["ready"]


# ---------------- 登录 ----------------

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def check_id_token(id_token: str, client_id: str, nonce: str | None, now: float | None = None) -> dict:
    """核对 ID 令牌：签发方、对象（就是这个登记号）、有效期、nonce。令牌是从 OpenAI 的令牌接口经 HTTPS 直接拿到的，
    按 OpenID Connect 的规定可以不再验签名（3.1.3.7 第 6 条）。"""
    c = _jwt_claims(id_token)
    aud = c.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    now = time.time() if now is None else now
    if c.get("iss") != ISSUER:
        raise ChatGPTError("登录结果的签发方不对，没有保存", "fatal")
    if client_id not in auds:
        raise ChatGPTError("登录结果不是发给本软件的，没有保存", "fatal")
    if not isinstance(c.get("exp"), (int, float)) or c["exp"] < now - 60:
        raise ChatGPTError("登录结果已过期，请重新登录", "fatal")
    if nonce is not None and c.get("nonce") != nonce:
        raise ChatGPTError("登录结果和这次登录对不上（nonce），没有保存", "fatal")
    if not c.get("sub"):
        raise ChatGPTError("登录结果里没有账号信息，没有保存", "fatal")
    return c


def _post_form(url: str, form: dict, timeout: float = HTTP_TIMEOUT) -> dict:
    req = Request(url, data=urlencode(form).encode("ascii"), method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with _urlopen(req, timeout) as r:
            body = r.read()
    except HTTPError as e:
        err = _error_of(e.read())
        raise ChatGPTError(str(err.get("message") or f"HTTP {e.code}"), "page", str(err.get("code") or ""), e.code) from e
    except (URLError, OSError) as e:
        raise ChatGPTError(f"连不上 OpenAI（{e}）", "retry") from e
    return json.loads(body) if body.strip() else {}


def _store_tokens(d: dict, tok: dict) -> None:
    now = time.time()
    d.update(access_token=tok.get("access_token"), token_type=tok.get("token_type") or "Bearer",
             expires_at=now + float(tok.get("expires_in") or 3600), saved_at=now)
    if tok.get("refresh_token"):
        d["refresh_token"] = tok["refresh_token"]  # 刷新令牌每次都换新的，用最新的
    if tok.get("id_token"):
        d["id_token"] = tok["id_token"]
    scope = tok.get("scope") or _jwt_claims(tok.get("access_token") or "").get("scope")
    if scope:
        d["scopes"] = scope
    if tok.get("earliest_refresh_at"):
        d["earliest_refresh_at"] = tok["earliest_refresh_at"]


@dataclass
class Login:
    """一次正在进行的登录：本机回调服务 + 浏览器里的授权页。"""
    url: str
    redirect_uri: str
    client_id: str
    state: str
    nonce: str
    verifier: str
    new_registration: bool
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    email: str = ""
    httpd: ThreadingHTTPServer | None = None

    def wait(self, timeout: float | None = LOGIN_TIMEOUT) -> bool:
        return self.done.wait(timeout)

    def close(self) -> None:
        httpd, self.httpd = self.httpd, None
        if httpd:
            def stop():
                httpd.shutdown()
                httpd.server_close()
            threading.Thread(target=stop, daemon=True).start()


_current: Login | None = None


def _login_state() -> dict | None:
    lg = _current
    if lg is None:
        return None
    return {"running": not lg.done.is_set(), "error": lg.error, "email": lg.email}


def _finish(lg: Login, params: dict) -> None:
    """浏览器回到本机：核对、换令牌、保存。出错写进 lg.error。"""
    if params.get("state") != lg.state:
        raise ChatGPTError("登录回调和这次登录对不上（state），请重新登录", "fatal")
    if params.get("error"):
        if params["error"] == "access_denied":
            raise ChatGPTError("在授权页上没有同意，登录取消了", "fatal")
        raise ChatGPTError(f"登录没有完成：{params.get('error_description') or params['error']}", "fatal")
    code = params.get("code")
    if not code:
        raise ChatGPTError("登录回调里没有授权码，请重新登录", "fatal")
    client_id = lg.client_id
    issued = params.get("client_id")
    if lg.new_registration:
        if not issued or issued == REGISTER_CLIENT:
            raise ChatGPTError("OpenAI 没有发登记号，请重新登录", "fatal")
        client_id = issued
    elif issued and issued != client_id:
        raise ChatGPTError("登录回调里的登记号和本机保存的不一样，没有保存", "fatal")
    try:
        tok = _post_form(TOKEN_URL, {"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                     "code_verifier": lg.verifier, "redirect_uri": lg.redirect_uri, "resource": API})
    except ChatGPTError as e:
        raise ChatGPTError(f"用授权码换令牌没有成功，请重新登录（{e.code or e}）", "fatal") from e
    if not tok.get("access_token"):
        raise ChatGPTError("OpenAI 没有给出令牌，请重新登录", "fatal")
    claims = check_id_token(tok.get("id_token") or "", client_id, lg.nonce)
    with _lock:
        d = _load()
        if not lg.new_registration and d.get("subject") and d["subject"] != claims["sub"]:
            raise ChatGPTError("登录的不是原来那个 ChatGPT 账号；要换账号，请在本软件里选“换个账号登录”", "fatal")
        if lg.new_registration:  # 新登记：换掉原来的账号信息，只留电脑编号
            d = {"host_id": d.get("host_id") or host_id()}
        d.update(client_id=client_id, issuer=ISSUER, subject=claims["sub"], email=claims.get("email") or "")
        d.pop("scopes", None)
        _store_tokens(d, tok)
        _save(d)
    lg.email = d["email"]
    log.info("ChatGPT 登录成功（%s，%s）", "新登记" if lg.new_registration else "原登记",
             "能用套餐额度" if PLAN_SCOPE in (d.get("scopes") or "").split() else "没有授权套餐额度")


def _handler(lg: Login):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 网址里有授权码，不打印
            pass

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path != CALLBACK_PATH:
                self.send_error(404)
                return
            params = {k: v[0] for k, v in parse_qs(u.query).items()}
            if lg.done.is_set():
                ok, text = lg.error is None, "这次登录已经处理过了，可以关掉这个页面。"
            else:
                try:
                    _finish(lg, params)
                    ok, text = True, "已用 ChatGPT 账号登录 bidbench。可以关掉这个页面，回到工作台。"
                except ChatGPTError as e:
                    lg.error = str(e)
                    ok, text = False, f"登录没有成功：{e}"
                except Exception as e:  # noqa: BLE001 - 出什么错都要让等着的人知道
                    log.exception("ChatGPT 登录回调出错")
                    lg.error = f"登录没有成功：{type(e).__name__}"
                    ok, text = False, lg.error
                lg.done.set()
            html = (f"<!doctype html><meta charset='utf-8'><title>bidbench</title>"
                    f"<body style='font-family:sans-serif;padding:40px'><h2>{'✓' if ok else '✗'} {text}</h2>")
            data = html.encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            lg.close()
    return Handler


def start_login(new_account: bool = False, open_browser=webbrowser.open) -> Login:
    """开始登录：起本机回调服务，在浏览器里打开授权页。new_account：不用原来的登记，换个账号重新登记。
    返回后在浏览器里完成；Login.wait() 等结果。"""
    global _current
    with _lock:
        if _current is not None and not _current.done.is_set():
            _current.error = "又开始了一次新的登录，这次作废"
            _current.done.set()
            _current.close()
        d = _load()
        hid = host_id()
        client_id = None if new_account else d.get("client_id")
        httpd = None
        for port in PORTS:
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
                break
            except OSError:
                continue
        if httpd is None:
            raise ChatGPTError("本机找不到可用的端口来接收登录结果", "fatal")
        redirect = f"http://127.0.0.1:{httpd.server_address[1]}{CALLBACK_PATH}"
        verifier = _b64url(secrets.token_bytes(48))
        q = {"response_type": "code", "client_id": client_id or REGISTER_CLIENT, "redirect_uri": redirect,
             "scope": SCOPES, "resource": API, "state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
             "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
             "code_challenge_method": "S256", "ext_agent_host_id": hid}
        if client_id:
            if d.get("id_token"):
                q["id_token_hint"] = d["id_token"]
            if d.get("email"):
                q["login_hint"] = d["email"]
        else:
            q["agent_name_hint"] = APP_NAME
        lg = Login(url=f"{AUTHORIZE_URL}?{urlencode(q)}", redirect_uri=redirect, client_id=client_id or REGISTER_CLIENT,
                   state=q["state"], nonce=q["nonce"], verifier=verifier, new_registration=not client_id)
        httpd.RequestHandlerClass = _handler(lg)
        lg.httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True, name="chatgpt-login").start()

        def expire():
            if not lg.done.wait(LOGIN_TIMEOUT):
                lg.error = "等了十分钟还没有在浏览器里登录完，这次登录作废了"
                lg.done.set()
                lg.close()
        threading.Thread(target=expire, daemon=True).start()
        _current = lg
    log.info("开始 ChatGPT 登录（%s，回调端口 %s）", "新登记" if lg.new_registration else "原登记",
             httpd.server_address[1])  # 网址里有 id_token_hint，不记
    try:
        open_browser(lg.url)
    except Exception as e:  # noqa: BLE001 - 打不开浏览器就让用户自己复制网址
        log.info("没能打开浏览器：%s", type(e).__name__)
    return lg


def logout() -> dict:
    """退出登录：先请 OpenAI 吊销刷新令牌，再删本机的令牌；登记号和电脑编号留着。"""
    with _lock:
        d = _load()
        rt, client_id = d.get("refresh_token"), d.get("client_id")
        revoked = None
        if rt and client_id:
            revoked = False
            for wait in (0, *RETRY_WAITS[:2]):
                if wait:
                    _sleep(wait)
                try:
                    disc = _get_json(DISCOVERY_URL, None)
                    url = disc.get("revocation_endpoint")
                    if not isinstance(url, str) or not url.startswith(ISSUER + "/"):
                        break
                    _post_form(url, {"token": rt, "token_type_hint": "refresh_token", "client_id": client_id})
                    revoked = True
                    break
                except ChatGPTError as e:
                    if e.kind != "retry" and not (e.status >= 500):
                        break
        for k in ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at", "scopes",
                  "token_type", "saved_at"):
            d.pop(k, None)
        _save(d)
    log.info("ChatGPT 退出登录（远端吊销：%s）", {True: "已确认", False: "没有确认", None: "不需要"}[revoked])
    return {"revoked": revoked, "status": status()}


# ---------------- 令牌 ----------------

class _FileLock:
    """几个进程（工作台、命令行的整夜识别）同时刷新会把轮换的刷新令牌用废，用一个锁文件排队。"""

    def __init__(self, stale: float = 120):
        self.path, self.stale = credentials_path().with_suffix(".lock"), stale

    def __enter__(self):
        t0 = time.monotonic()
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.monotonic() - t0 > self.stale:
                    raise ChatGPTError("等不到别的程序刷新完 ChatGPT 登录", "retry") from None
                time.sleep(0.2)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def access_token(force_refresh: bool = False) -> str:
    """能用的访问令牌：快过期（或 force_refresh）就先刷新。没登录、刷新令牌失效抛 ChatGPTError(fatal)。"""
    with _lock:
        d = _load()
        if not d.get("access_token") and not d.get("refresh_token"):
            raise ChatGPTError(NOT_SIGNED_IN + "：请先在工作台或命令行（bidbench chatgpt login）登录", "fatal")
        if PLAN_SCOPE not in (d.get("scopes") or "").split():
            raise ChatGPTError(status()["hint"] or NOT_SIGNED_IN, "fatal")
        now = time.time()
        fresh = d.get("access_token") and now < float(d.get("expires_at") or 0) - REFRESH_BEFORE
        if fresh and not force_refresh:
            return d["access_token"]
        if not d.get("refresh_token"):
            raise ChatGPTError("ChatGPT 登录已过期，请重新登录", "fatal")
        old = d.get("access_token")
        with _FileLock():
            d = _load()  # 别的进程可能刚刷新过
            if d.get("access_token") != old and now < float(d.get("expires_at") or 0) - REFRESH_BEFORE:
                return d["access_token"]
            try:
                tok = _post_form(TOKEN_URL, {"grant_type": "refresh_token", "client_id": d.get("client_id") or "",
                                             "refresh_token": d["refresh_token"], "resource": API})
            except ChatGPTError as e:
                if e.code in _REFRESH_DEAD or (e.status in (400, 401) and e.kind != "retry"):
                    for k in ("access_token", "refresh_token", "expires_at"):
                        d.pop(k, None)
                    _save(d)
                    raise ChatGPTError("ChatGPT 登录已失效，请重新登录", "fatal", e.code, e.status) from e
                if e.code == "invalid_client":
                    raise ChatGPTError("本机保存的登记号不能用了，请选“换个账号登录”重新登记", "fatal", e.code) from e
                if fresh:
                    return d["access_token"]
                raise ChatGPTError(f"刷新 ChatGPT 登录没有成功（{e}）", "retry", e.code, e.status) from e
            if not tok.get("access_token"):
                raise ChatGPTError("刷新 ChatGPT 登录没有拿到新令牌", "retry")
            _store_tokens(d, tok)
            _save(d)
            log.info("ChatGPT 登录已刷新")
            return d["access_token"]


# ---------------- 调用模型 ----------------

def _get_json(url: str, token: str | None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _urlopen(Request(url, headers=headers), HTTP_TIMEOUT) as r:
            return json.loads(r.read())
    except HTTPError as e:
        raise _api_error(e.code, _error_of(e.read())) from e
    except (URLError, OSError, json.JSONDecodeError) as e:
        raise ChatGPTError(f"连不上 OpenAI（{type(e).__name__}）", "retry") from e


def list_models() -> list[dict]:
    """这个账号能用的模型（/v1/models 里 visibility 为 list 的），能看图的才要；按推荐顺序。"""
    def get():
        return _get_json(f"{API}/models", access_token())
    try:
        d = get()
    except ChatGPTError as e:
        if e.kind != "auth":
            raise
        access_token(force_refresh=True)
        d = get()
    rows = (d.get("models") or d.get("data") or []) if isinstance(d, dict) else []
    out = []
    for m in rows:
        if not isinstance(m, dict) or m.get("visibility", "list") != "list":
            continue
        mods = m.get("input_modalities")
        if isinstance(mods, list) and "image" not in mods:
            continue
        slug = m.get("slug") or m.get("id")
        if not slug:
            continue
        out.append({"slug": slug, "name": m.get("display_name") or slug, "description": m.get("description") or "",
                    "efforts": [x.get("effort") for x in m.get("supported_reasoning_levels") or []
                                if isinstance(x, dict) and x.get("effort")],
                    "priority": m["priority"] if isinstance(m.get("priority"), (int, float)) else 99})
    out.sort(key=lambda m: m.pop("priority"))
    return out


@dataclass
class Reply:
    text: str
    usage: dict  # input_tokens / cached_tokens / output_tokens


def _sse(resp):
    """一行一行读 Server-Sent Events，交出每个事件的 JSON。"""
    data: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                data = []
                if payload.strip() == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        try:
            yield json.loads("\n".join(data))
        except json.JSONDecodeError:
            pass


def _output_text(response: dict) -> str:
    parts = []
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
    return "".join(parts)


def _stream_once(body: dict, token: str) -> Reply:
    req = Request(f"{API}/responses", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                           "Accept": "text/event-stream"})
    try:
        resp = _urlopen(req, STREAM_TIMEOUT)
    except HTTPError as e:
        raise _api_error(e.code, _error_of(e.read())) from e
    except (URLError, OSError) as e:
        raise ChatGPTError(f"连不上 OpenAI（{e}）", "retry") from e
    deltas: list[str] = []
    try:
        with resp:
            for ev in _sse(resp):
                t = ev.get("type")
                if t == "response.output_text.delta":
                    deltas.append(ev.get("delta") or "")
                elif t == "response.completed":
                    r = ev.get("response") or {}
                    u = r.get("usage") or {}
                    return Reply(_output_text(r) or "".join(deltas), {
                        "input_tokens": u.get("input_tokens") or 0, "output_tokens": u.get("output_tokens") or 0,
                        "cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens") or 0})
                elif t == "response.failed":
                    err = (ev.get("response") or {}).get("error") or {}
                    raise _api_error(0, err)
                elif t == "response.incomplete":
                    why = ((ev.get("response") or {}).get("incomplete_details") or {}).get("reason") or "未知原因"
                    raise ChatGPTError(f"模型没有答完（{why}）", "page", "incomplete")
                elif t == "error":
                    raise _api_error(0, ev.get("error") if isinstance(ev.get("error"), dict) else ev)
    except (OSError, ValueError) as e:  # 读到一半断了
        raise ChatGPTError(f"和 OpenAI 的连接中途断了（{type(e).__name__}）", "retry") from e
    raise ChatGPTError("OpenAI 的回答没有正常结束", "retry", "interrupted")


def respond(body: dict) -> Reply:
    """发一次 Responses 请求（store=false、stream=true），读到 response.completed 才算成功。
    登录失效先刷新再试一次；服务暂时不可用按 RETRY_WAITS 退避重试，还不行就当额度暂时用不了（kind=limit）。"""
    body = {**body, "store": False, "stream": True}
    refreshed = False
    waits = list(RETRY_WAITS)
    while True:
        try:
            return _stream_once(body, access_token())
        except ChatGPTError as e:
            if e.kind == "auth" and not refreshed:
                refreshed = True
                access_token(force_refresh=True)
                continue
            if e.kind == "auth":
                raise ChatGPTError(f"{e}。请重新登录 ChatGPT 账号", "fatal", e.code, e.status) from e
            if e.kind == "retry":
                if waits:
                    w = waits.pop(0)
                    log.info("OpenAI 暂时不可用（%s），%s 秒后重试", e.code or e.status or "网络", w)
                    _sleep(w)
                    continue
                raise ChatGPTError(f"{e}，重试几次都不行", "limit", e.code, e.status) from e
            raise


def image_part(png: bytes) -> dict:
    return {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "detail": "high"}


def ask(prompt: str, model: str, effort: str, schema: dict | None = None, images: list[bytes] = (),
        name: str = "result") -> Reply:
    """问一次模型：一段提示 + 若干张图，schema 给了就要求按这个格式（严格模式）输出 JSON。"""
    content = [{"type": "input_text", "text": prompt}, *[image_part(b) for b in images]]
    body: dict = {"model": model, "input": [{"role": "user", "content": content}], "reasoning": {"effort": effort}}
    if schema is not None:
        body["text"] = {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}
    return respond(body)

