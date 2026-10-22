"""ChatGPT 账号登录、直接调用 OpenAI：用假的 OpenAI（替换 chatgpt._urlopen），不联网、不占额度。"""

import base64
import hashlib
import io
import json
import logging
import os
import threading
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from bidbench import chatgpt, recognition, server
from bidbench.executors import claude, codex

from test_codex_executor import _body_from_prompt


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _sse(*events) -> _Resp:
    return _Resp("".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode())


def _http_error(url, code, body):
    return HTTPError(url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))


def _completed(text, usage=None):
    return {"type": "response.completed", "response": {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": usage or {"input_tokens": 3000, "input_tokens_details": {"cached_tokens": 1000}, "output_tokens": 500}}}


class FakeOpenAI:
    """假的 auth.openai.com 和 api.openai.com。answer(body, n) 返回 Responses 的事件列表，或抛 HTTPError。"""

    def __init__(self, answer=None, sub="user-1", email="lin@example.com"):
        self.sub, self.email = sub, email
        self.challenge = self.nonce = None
        self.requests, self.responses, self.revoked = [], [], []
        self.answer = answer or (lambda body, n: [_completed(json.dumps(_body_from_prompt(body["input"][0]["content"][0]["text"]), ensure_ascii=False))])
        self.refresh_n = 0
        self.dead_refresh = False
        self.models = [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list",
                        "input_modalities": ["text", "image"], "priority": 1},
                       {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "priority": 0},
                       {"slug": "hidden", "visibility": "hide"},
                       {"slug": "text-only", "visibility": "list", "input_modalities": ["text"]}]

    def authorize(self, url):
        """浏览器打开授权页：记下 PKCE 和 nonce。"""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.challenge, self.nonce, self.auth_q = q["code_challenge"], q["nonce"], q

    def tokens(self, client_id, scope=chatgpt.SCOPES, refresh="r1"):
        now = int(time.time())
        return {"access_token": _jwt({"sub": self.sub, "scope": scope, "exp": now + 3600}), "refresh_token": refresh,
                "id_token": _jwt({"iss": chatgpt.ISSUER, "aud": client_id, "sub": self.sub, "email": self.email,
                                  "exp": now + 3600, "nonce": self.nonce}),
                "token_type": "Bearer", "expires_in": 3600, "scope": scope}

    def __call__(self, req, timeout):
        url = req.full_url
        data = req.data.decode() if req.data else ""
        self.requests.append((url, data))
        if url == chatgpt.TOKEN_URL:
            f = {k: v[0] for k, v in parse_qs(data).items()}
            assert f["resource"] == chatgpt.API
            if f["grant_type"] == "authorization_code":
                digest = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                assert digest == self.challenge and f["code"] == "the-code"
                return _Resp(json.dumps(self.tokens(f["client_id"])).encode())
            assert f["grant_type"] == "refresh_token" and f["client_id"].startswith("oaiapp_") and "scope" not in f
            if self.dead_refresh:
                raise _http_error(url, 400, {"error": "refresh_token_reused"})
            self.refresh_n += 1
            return _Resp(json.dumps(self.tokens(f["client_id"], refresh=f"r{self.refresh_n + 1}")).encode())
        if url == chatgpt.DISCOVERY_URL:
            return _Resp(json.dumps({"revocation_endpoint": chatgpt.ISSUER + "/oauth/revoke"}).encode())
        if url == chatgpt.ISSUER + "/oauth/revoke":
            self.revoked.append(parse_qs(data))
            return _Resp(b"")
        assert req.get_header("Authorization", "").startswith("Bearer ")
        if url == chatgpt.API + "/models":
            return _Resp(json.dumps({"models": self.models}).encode())
        if url == chatgpt.API + "/responses":
            body = json.loads(data)
            self.responses.append(body)
            return _sse(*self.answer(body, len(self.responses)))
        raise AssertionError(url)


def _browser_returns(lg, **params):
    """像浏览器一样回到本机的回调地址。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = opener.open(lg.redirect_uri + "?" + urlencode(params))
        return r.status, r.read().decode()
    except HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def fake(monkeypatch):
    f = FakeOpenAI()
    monkeypatch.setattr(chatgpt, "_urlopen", f)
    monkeypatch.setattr(chatgpt, "_sleep", lambda s: None)
    yield f
    if chatgpt._current is not None:  # 关掉最后一次登录的回调服务，不留给下一个测试
        chatgpt._current.close(wait=True)


def _login(fake, new_account=False, **extra):
    lg = chatgpt.start_login(new_account=new_account, open_browser=fake.authorize)
    q = {"code": "the-code", "state": fake.auth_q["state"], **extra}
    if fake.auth_q["client_id"] == chatgpt.REGISTER_CLIENT:
        q.setdefault("client_id", "oaiapp_123")
    code, page = _browser_returns(lg, **q)
    assert lg.wait(5)
    return lg, code, page


def test_login_registers_and_saves(fake, caplog):
    caplog.set_level(logging.DEBUG)
    assert not chatgpt.status()["signed_in"]
    lg, code, page = _login(fake)
    assert code == 200 and "已用 ChatGPT 账号登录" in page and lg.error is None
    q = fake.auth_q
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "bidbench"
    assert q["scope"] == chatgpt.SCOPES and q["resource"] == "https://api.openai.com/v1"
    assert q["code_challenge_method"] == "S256" and q["ext_agent_host_id"].startswith("urn:uuid:")
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith("/auth/callback")
    st = chatgpt.status()
    assert st["ready"] and st["email"] == "lin@example.com" and st["login"]["running"] is False
    saved = json.loads(chatgpt.credentials_path().read_text(encoding="utf-8"))
    assert saved["client_id"] == "oaiapp_123" and saved["host_id"] == q["ext_agent_host_id"]
    if os.name != "nt":
        assert oct(chatgpt.credentials_path().stat().st_mode & 0o777) == "0o600"  # 只有本人能读
    logged = caplog.text
    assert saved["refresh_token"] not in logged and saved["access_token"] not in logged and "the-code" not in logged

    # 再登录：用原来的登记号和电脑编号，不再登记
    lg, code, _ = _login(fake)
    q2 = fake.auth_q
    assert code == 200 and q2["client_id"] == "oaiapp_123" and "agent_name_hint" not in q2
    assert q2["ext_agent_host_id"] == q["ext_agent_host_id"] and q2["id_token_hint"] and q2["login_hint"] == "lin@example.com"
    # 原登记号登进了别的账号：不保存
    fake.sub = "someone-else"
    lg, code, page = _login(fake)
    assert code == 400 and "不是原来那个" in lg.error and chatgpt.status()["email"] == "lin@example.com"
    # 换个账号：重新登记
    lg, code, _ = _login(fake, new_account=True, client_id="oaiapp_456")
    assert code == 200 and fake.auth_q["client_id"] == "dynamic_agent_client"
    assert json.loads(chatgpt.credentials_path().read_text(encoding="utf-8"))["client_id"] == "oaiapp_456"


def test_login_rejects_bad_callbacks(fake):
    lg = chatgpt.start_login(open_browser=fake.authorize)
    code, page = _browser_returns(lg, code="the-code", state="forged", client_id="oaiapp_1")
    assert code == 400 and "state" in lg.error and not chatgpt.status()["signed_in"]
    assert not any(u == chatgpt.TOKEN_URL for u, _ in fake.requests)  # 没换令牌

    lg = chatgpt.start_login(open_browser=fake.authorize)
    code, _ = _browser_returns(lg, error="access_denied", state=fake.auth_q["state"])
    assert code == 400 and "没有同意" in lg.error

    lg, code, _ = _login(fake, client_id=chatgpt.REGISTER_CLIENT)  # 新登记却没发登记号
    assert code == 400 and "登记号" in lg.error


def test_login_without_plan_scope(fake, monkeypatch):
    real = fake.tokens
    monkeypatch.setattr(fake, "tokens", lambda cid, **k: real(cid, scope="openid profile email offline_access"))
    _login(fake)
    st = chatgpt.status()
    assert st["signed_in"] and not st["ready"] and "没有同意使用 ChatGPT 套餐额度" in st["hint"]
    with pytest.raises(chatgpt.ChatGPTError) as e:
        chatgpt.access_token()
    assert e.value.kind == "fatal"


def test_check_id_token():
    now = time.time()
    good = {"iss": chatgpt.ISSUER, "aud": "oaiapp_1", "sub": "u", "exp": now + 60, "nonce": "n"}
    assert chatgpt.check_id_token(_jwt(good), "oaiapp_1", "n")["sub"] == "u"
    for bad, word in [({"iss": "https://evil.example"}, "签发方"), ({"aud": "oaiapp_2"}, "不是发给"),
                      ({"exp": now - 3600}, "过期"), ({"nonce": "x"}, "nonce")]:
        with pytest.raises(chatgpt.ChatGPTError, match=word):
            chatgpt.check_id_token(_jwt({**good, **bad}), "oaiapp_1", "n")


def _expire():
    d = chatgpt._load()
    d["expires_at"] = time.time() - 10
    chatgpt._save(d)


def test_refresh_rotates_and_dead_refresh_signs_out(fake):
    _login(fake)
    first = chatgpt.access_token()
    assert chatgpt.access_token() == first and fake.refresh_n == 0  # 没到期不刷新
    _expire()
    assert chatgpt.access_token() and fake.refresh_n == 1
    assert chatgpt._load()["refresh_token"] == "r2"  # 刷新令牌换成新的
    assert not chatgpt.credentials_path().with_suffix(".lock").exists()
    _expire()
    fake.dead_refresh = True
    with pytest.raises(chatgpt.ChatGPTError, match="重新登录") as e:
        chatgpt.access_token()
    assert e.value.kind == "fatal" and not chatgpt.status()["signed_in"]


def test_respond_stream_and_errors(fake):
    _login(fake)
    fake.answer = lambda body, n: [{"type": "response.output_text.delta", "delta": "{\"a\""},
                                   {"type": "response.output_text.delta", "delta": ": 1}"}, _completed("{\"a\": 1}")]
    r = chatgpt.ask("你好", "gpt-6.1-sol", "low", {"type": "object"}, [b"\x89PNG"])
    assert r.text == '{"a": 1}' and r.usage == {"input_tokens": 3000, "output_tokens": 500, "cached_tokens": 1000}
    body = fake.responses[-1]
    assert body["store"] is False and body["stream"] is True and body["reasoning"] == {"effort": "low"}
    assert body["text"]["format"]["strict"] is True and body["input"][0]["content"][1]["type"] == "input_image"
    assert not set(body) & {"temperature", "max_output_tokens", "previous_response_id", "metadata", "user", "top_p"}

    # 额度用完：开始回答以后才说
    fake.answer = lambda body, n: [{"type": "response.created"}, {"type": "response.failed", "response": {
        "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}]
    with pytest.raises(chatgpt.ChatGPTError) as e:
        chatgpt.ask("x", "m", "low")
    assert e.value.kind == "limit" and "用量额度暂时用完" in str(e.value)

    # 不能用套餐：不重试
    def not_eligible(body, n):
        raise _http_error(chatgpt.API + "/responses", 403, {"error": {"code": "subscription_sharing_user_not_eligible"}})
    fake.answer, before = not_eligible, len(fake.responses)
    with pytest.raises(chatgpt.ChatGPTError) as e:
        chatgpt.ask("x", "m", "low")
    assert e.value.kind == "fatal" and len(fake.responses) == before + 1

    # 401：刷新一次再试；503：退避重试
    seq = iter([401, 503, None])

    def flaky(body, n):
        code = next(seq)
        if code:
            raise _http_error(chatgpt.API + "/responses", code, {"error": {"message": "x"}})
        return [_completed("ok")]
    fake.answer, refresh0 = flaky, fake.refresh_n
    assert chatgpt.ask("x", "m", "low").text == "ok" and fake.refresh_n == refresh0 + 1

    # 一直 503：重试几次后当作暂时用不了（整夜识别会等着续跑）
    fake.answer = lambda body, n: (_ for _ in ()).throw(_http_error(chatgpt.API + "/responses", 503, {}))
    with pytest.raises(chatgpt.ChatGPTError) as e:
        chatgpt.ask("x", "m", "low")
    assert e.value.kind == "limit"

    # 没有 response.completed 就断了
    fake.answer = lambda body, n: [{"type": "response.output_text.delta", "delta": "{"}]
    with pytest.raises(chatgpt.ChatGPTError) as e:
        chatgpt.ask("x", "m", "low")
    assert e.value.kind == "limit"


def test_logout_revokes_and_keeps_registration(fake):
    _login(fake)
    r = chatgpt.logout()
    assert r["revoked"] is True and fake.revoked[0]["token"] == ["r1"] and fake.revoked[0]["client_id"] == ["oaiapp_123"]
    d = chatgpt._load()
    assert not chatgpt.status()["signed_in"] and "refresh_token" not in d and "id_token" not in d
    assert d["client_id"] == "oaiapp_123" and d["host_id"].startswith("urn:uuid:")
    assert chatgpt.logout()["revoked"] is None


def test_codex_executor_uses_chatgpt_directly(fake, demo_copy, monkeypatch):
    monkeypatch.setattr(codex, "_candidates", lambda: [])  # 本机没有 Codex 命令行也能用
    task = recognition.make_task(demo_copy, scope="conflict")
    pages = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    assert codex.status(refresh=True)["ready"] is False
    _login(fake)
    st = codex.status(refresh=True)
    assert st["ready"] and st["backend"] == "chatgpt" and not st["cli_ready"] and st["hint"] is None
    assert [m["slug"] for m in st["models"]] == ["gpt-6-astra", "gpt-6.1-sol"]  # 隐藏的、不能看图的不要
    assert server._chat_codex(st)["ready"] is False  # AI 助手还要靠 Codex 命令行

    lines = []
    r = codex.run(task, "economy", workers=2, log_fn=lines.append)
    assert r.done == len(pages) and not r.failed
    assert lines[0].startswith(f"开始识别 {len(pages)} 页：ChatGPT 账号直连（lin@example.com），模型 gpt-6.1-sol")
    body = fake.responses[0]
    content = body["input"][0]["content"]
    assert body["model"] == "gpt-6.1-sol" and body["reasoning"] == {"effort": "medium"}
    assert claude.SYSTEM_PROMPT in content[0]["text"] and [c["type"] for c in content[1:]] == ["input_image"] * 2
    assert body["text"]["format"]["schema"] == codex.output_schema()
    log = (task / "results" / "run_log.md").read_text(encoding="utf-8")
    assert "ChatGPT 账号直连" in log and "ChatGPT 套餐的额度" in log
    usage = json.loads((task / "results" / "usage.json").read_text(encoding="utf-8"))
    assert usage["via"] == "chatgpt" and usage["cached_input_tokens"] == 1000 * len(pages)
    res = recognition.import_results(demo_copy, task / "results")
    assert res.imported == len(pages) and not res.errors

    # 智能分类也直连
    fake.answer = lambda body, n: [_completed(json.dumps({"ok": True}))]
    assert codex.ask_json("分一下", {"type": "object", "properties": {"ok": {"type": "boolean"}}}) == {"ok": True}
    assert fake.responses[-1]["text"]["format"]["schema"]["required"] == ["ok"]

    # 额度用完：整次停下，留着续跑
    fake.answer = lambda body, n: [{"type": "response.failed", "response": {
        "error": {"code": "subscription_sharing_usage_limit_exceeded"}}}]
    task2 = recognition.make_task(demo_copy, scope="all")
    with pytest.raises(claude.LimitError, match="续跑"):
        codex.run(task2, "economy", workers=1)

    # 强制用命令行
    monkeypatch.setenv("BIDBENCH_OPENAI", "cli")
    assert codex.status(refresh=True)["backend"] == "cli"


def test_workbench_login_endpoints(fake, demo_copy, monkeypatch):
    from test_desktop import Client
    opened = []
    real = chatgpt.start_login
    monkeypatch.setattr(chatgpt, "start_login", lambda new_account=False: real(new_account, opened.append))
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        assert c.post("/api/app/chatgpt/login", {}, token=False)["_status"] == 403  # 要令牌
        r = c.post("/api/app/chatgpt/login", {})
        assert r["url"].startswith(chatgpt.AUTHORIZE_URL) and opened == [r["url"]] and r["login"]["running"]
        fake.authorize(r["url"])
        lg = chatgpt._current
        assert _browser_returns(lg, code="the-code", state=fake.auth_q["state"], client_id="oaiapp_9")[0] == 200
        st = c.get("/api/chatgpt")
        assert st["ready"] and st["login"] == {"running": False, "error": None, "email": "lin@example.com"}
        assert c.get("/api/app")["settings"]["chatgpt"]["ready"]
        assert "refresh_token" not in json.dumps(c.get("/api/app"))
        assert c.post("/api/app/chatgpt/logout", {})["revoked"] is True
    finally:
        httpd.shutdown()
