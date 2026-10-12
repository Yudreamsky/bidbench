"""Claude 执行器测试：用假的 API 客户端，不联网、不花钱。"""

import json
from types import SimpleNamespace

import pytest

from bidbench import recognition, state
from bidbench.executors import claude, simulated


class FakeClient:
    """模拟 messages.stream / beta.messages.stream。responder(params, call_no) 返回 (正文 dict 或 str, stop_reason)。"""

    def __init__(self, responder):
        self.calls = []
        self.responder = responder
        self.messages = SimpleNamespace(stream=lambda **p: self._stream(p, beta=False))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **p: self._stream(p, beta=True)))

    def _stream(self, params, beta):
        self.calls.append({"beta": beta, **params})
        body, stop = self.responder(params, len(self.calls))
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model=params["model"],
            stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
            usage=SimpleNamespace(input_tokens=1200, output_tokens=600, cache_read_input_tokens=800,
                                  cache_creation_input_tokens=0))

        class _S:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def get_final_message(self_inner):
                return msg
        return _S()


def _page_text(params) -> str:
    content = params["messages"][0]["content"]
    return content[-1]["text"]


def mimic(params, _n):
    """把文字层喂给规则执行器，生成“像模型一样”的正文，来源标为 both。"""
    raw = _page_text(params)
    lines = []
    for ln in raw.splitlines()[1:]:
        if ln.startswith("[") and "] " in ln:
            box, t = ln[1:].split("] ", 1)
            lines.append({"text": t, "bbox": [float(x) for x in box.split(",")]})
    pid = params["messages"][0]["content"][0]["text"].split()[1].rstrip("，")
    item = {"page_id": pid, "source": {"file_path": "x", "file_sha256": "0" * 64, "page_index": 1,
                                       "page_count": 1, "has_text_layer": bool(lines)}}
    rec = simulated.recognize_page(item, lines)
    body = {k: rec[k] for k in claude.BODY_KEYS}
    return json.loads(json.dumps(body).replace('"text_layer"', '"both"')), "end_turn"


def test_body_schema_is_api_compatible():
    s = json.dumps(claude.body_schema(), ensure_ascii=False)
    for bad in ('"minimum"', '"maximum"', '"minItems"', '"maxItems"', '"pattern"', '"oneOf"', '"const"', '"type": ['):
        assert bad not in s, bad


def test_scopes(demo_copy):
    with demo_copy.connect() as con:
        assert recognition.pages_to_recognize(con, scope="unrecognized") == []
        new = recognition.pages_to_recognize(con, scope="new")      # 全是模拟结果，都要重新识别
        allp = recognition.pages_to_recognize(con, scope="all")
        weak = recognition.pages_to_recognize(con, scope="weak")
    assert len(new) == len(allp) == len(weak) > 0
    assert len({r["render_hash"] for r in allp}) == len(allp)


def test_economy_end_to_end(demo_copy):
    task = recognition.make_task(demo_copy, scope="new")
    n_pages = len(json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"])
    fake = FakeClient(mimic)
    r = claude.run(task, "economy", client=fake, workers=3)
    assert r.done == n_pages and not r.failed
    call = fake.calls[0]
    assert call["beta"] is False and call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    images = [b for b in call["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 2  # 整页 + 图签，无切片
    log = (task / "results" / "run_log.md").read_text(encoding="utf-8")
    assert "估算花费" in log and "问题：无" in log

    res = recognition.import_results(demo_copy, task / "results")
    assert res.imported == n_pages and not res.errors
    st = state.build(demo_copy)
    assert st["recognition"]["executors"] == {"claude": n_pages}
    facts = {f["key"]: f for g in st["facts"]["groups"] for f in g["facts"]}
    assert facts["地上层数"]["conf"] == "conflict"  # 换执行器后结论不变
    # 再挑 new：真实结果已覆盖模拟结果，不再需要识别
    with demo_copy.connect() as con:
        assert recognition.pages_to_recognize(con, scope="new") == []

    # 续跑：结果已存在且合格，全部跳过
    r2 = claude.run(task, "economy", client=FakeClient(mimic))
    assert r2.done == 0 and r2.skipped == n_pages


def test_strong_tier_conflict_scope(demo_copy):
    task = recognition.make_task(demo_copy, scope="conflict")
    pages = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"]
    files = {p["source"]["file_path"] for p in pages}
    assert any("电施-01" in f for f in files)          # 版本冲突的两份
    assert any("结施-01" in f for f in files)          # 事实冲突的出处
    fake = FakeClient(mimic)
    r = claude.run(task, "strong", client=fake, workers=2)
    assert r.done == len(pages)
    call = fake.calls[0]
    assert call["beta"] is True and call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["effort"] == "high"
    assert len([b for b in call["messages"][0]["content"] if b["type"] == "image"]) == 2 + 9


def test_retry_after_validation_error(demo_copy):
    task = recognition.make_task(demo_copy, scope="new")

    def bad_then_good(params, n):
        body, stop = mimic(params, n)
        if len(params["messages"]) == 1:
            body["page_kind"] = "图"  # 不在枚举里
        return body, stop

    fake = FakeClient(bad_then_good)
    r = claude.run(task, "economy", client=fake, workers=1)
    assert not r.failed and r.done > 0
    retry = next(c for c in fake.calls if len(c["messages"]) == 3)
    assert retry["messages"][1]["role"] == "assistant" and "没有通过校验" in retry["messages"][2]["content"]


def test_refusal_and_bad_json_are_recorded(demo_copy):
    task = recognition.make_task(demo_copy, scope="new")

    def flaky(params, n):
        if n == 1:
            return {}, "refusal"
        if n == 2:
            return "不是 JSON", "end_turn"
        return mimic(params, n)

    r = claude.run(task, "economy", client=FakeClient(flaky), workers=1)
    assert len(r.failed) == 2
    assert any("拒绝" in f["error"] for f in r.failed) and any("JSON" in f["error"] for f in r.failed)
    log = (task / "results" / "run_log.md").read_text(encoding="utf-8")
    assert "失败页面" in log
    # 只导入成功的页面
    res = recognition.import_results(demo_copy, task / "results")
    assert res.imported == r.done


def test_sanitize_clamps_and_drops_bad_bbox():
    out = claude._sanitize({"a": {"confidence": 1.7, "bbox": [0.1, 0.2, 1.3, 0.9]}, "b": {"bbox": [1, 2]},
                            "c": [{"confidence": -0.2, "bbox": None}]})
    assert out["a"] == {"confidence": 1.0, "bbox": [0.1, 0.2, 1.0, 0.9]}
    assert out["b"]["bbox"] is None and out["c"][0]["confidence"] == 0.0


def test_long_text_layer_is_truncated_with_note(demo_copy, monkeypatch):
    task = recognition.make_task(demo_copy, scope="new", render=False)
    item = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"][0]
    path = task / item["text_lines"]
    path.write_text(json.dumps({"lines": [{"text": f"第{i}行说明文字" * 5, "bbox": [0, 0, 1, 1]} for i in range(3000)]},
                               ensure_ascii=False), encoding="utf-8")
    content = claude.build_content(task, item, tiles=0)
    assert "过长" in content[-1]["text"] and len(content[-1]["text"]) < claude.MAX_TEXT_CHARS + 500


def test_estimate(demo_copy):
    task = recognition.make_task(demo_copy, scope="new")
    e = claude.estimate(task, "economy")
    s = claude.estimate(task, "strong")
    assert e["pages"] == s["pages"] and s["images"] > e["images"]
    assert 0 < e["estimated_usd"] < s["estimated_usd"]


@pytest.mark.parametrize("tier", ["economy", "strong"])
def test_tiers_have_prices(tier):
    assert claude.TIERS[tier]["model"] in claude.PRICES


class BadRequestError(Exception):
    """名字与 anthropic.BadRequestError 相同，用来模拟 API 拒绝 schema。"""


def test_falls_back_when_schema_rejected(demo_copy):
    task = recognition.make_task(demo_copy, scope="new")

    class Rejecting(FakeClient):
        def _stream(self, params, beta):
            if "format" in params["output_config"]:
                self.calls.append({"rejected": True})
                raise BadRequestError("output_config.format.schema: unsupported keyword")
            return super()._stream(params, beta)

    def fenced(params, n):
        body, stop = mimic(params, n)
        return "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```", stop

    fake = Rejecting(fenced)
    r = claude.run(task, "economy", client=fake, workers=1)
    assert not r.failed and r.done > 0 and r.structured is False
    assert sum(1 for c in fake.calls if c.get("rejected")) == 1  # 只试一次，之后整次运行都用兜底方式
    ok = next(c for c in fake.calls if not c.get("rejected"))
    assert "JSON Schema" in ok["messages"][0]["content"][-1]["text"]
    assert "不接受这份结构化输出格式" in (task / "results" / "run_log.md").read_text(encoding="utf-8")


class AuthenticationError(Exception):
    pass


def test_missing_credentials_stop_the_run(demo_copy):
    task = recognition.make_task(demo_copy, scope="new", render=False)

    class NoKey(FakeClient):
        def _stream(self, params, beta):
            self.calls.append(params)
            raise AuthenticationError("invalid x-api-key")

    fake = NoKey(mimic)
    with pytest.raises(claude.FatalError, match="ANTHROPIC_API_KEY"):
        claude.run(task, "economy", client=fake, workers=4)
    assert len(fake.calls) == 1  # 第一页就停下，没有把每一页都试一遍


def test_real_result_wins_over_simulated_for_identical_pages(demo_copy):
    """像素相同的两页：一页只有模拟结果，另一页有真实结果时，两页都用真实结果，也不再挑进 new。"""
    with demo_copy.connect() as con:
        a = con.execute("SELECT * FROM pages ORDER BY page_id LIMIT 1").fetchone()
        twin = "ffffffffffff-p001"
        con.execute("INSERT INTO pages(page_id, file_sha256, page_index, width, height, has_text, render_hash, dhash, text)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (twin, a["file_sha256"], 99, a["width"], a["height"], a["has_text"],
                                                    a["render_hash"], a["dhash"], a["text"]))
        rec = json.loads(con.execute("SELECT data FROM recognitions WHERE page_id=?", (a["page_id"],)).fetchone()["data"])
        assert rec["run"]["executor"] == "simulated"
        rec["run"]["executor"], rec["page_id"] = "claude", twin
        con.execute("INSERT INTO recognitions(page_id, executor, model, instruction_version, finished_at, imported_at, data)"
                    " VALUES (?,?,?,?,?,?,?)", (twin, "claude", "m", "0.2", "t", "t", json.dumps(rec, ensure_ascii=False)))
    from bidbench.state import _load
    with demo_copy.connect() as con:
        pages = {p.page_id: p for p in _load(con, 4)}
    assert pages[a["page_id"]].rec["run"]["executor"] == "claude" and pages[a["page_id"]].rec_borrowed
    with demo_copy.connect() as con:
        assert not any(r["render_hash"] == a["render_hash"] for r in recognition.pages_to_recognize(con, scope="new"))


def test_progress_log_line_per_page(demo_copy):
    """识别过程日志：每页一行实时给界面，同时追加到任务包的 results/progress.log，续跑时接着写。"""
    task = recognition.make_task(demo_copy, scope="new", render=False)
    n_pages = len(json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))["pages"])

    def flaky(params, n):
        if n == 1:
            return {}, "refusal"
        body, stop = mimic(params, n)
        if n == 2:
            body["page_kind"] = "图"  # 第一次没通过校验，重试
        return body, stop

    lines = []
    r = claude.run(task, "economy", client=FakeClient(flaky), workers=1, log_fn=lines.append)
    assert r.done == n_pages - 1 and len(r.failed) == 1
    assert lines[0].startswith(f"开始识别 {n_pages} 页：模型 claude-haiku-5-5")
    assert sum(1 for ln in lines if "✓" in ln) == n_pages - 1
    bad = next(ln for ln in lines if "✗" in ln)
    assert bad.startswith(f"  ✗ 1/{n_pages} ") and "拒绝识别" in bad
    assert any("↻" in ln and "重试一次" in ln for ln in lines)
    ok = next(ln for ln in lines if "✓" in ln)
    assert "第 1/" in ok and " → " in ok and "输入 2400 / 输出 1200 tokens" in ok  # 重试那页调用了两次
    saved = (task / "results" / "progress.log").read_text(encoding="utf-8").splitlines()
    assert len(saved) == len(lines) + 1 and f"结束：识别 {n_pages - 1} 页，失败 1 页，用时" in saved[-1]

    # 续跑：只重试失败的那一页，progress.log 接着写
    more = []
    r2 = claude.run(task, "economy", client=FakeClient(mimic), workers=1, log_fn=more.append)
    assert r2.done == 1 and r2.skipped == n_pages - 1
    assert more[0] == f"开始识别 1 页（已有结果的 {n_pages - 1} 页跳过）：模型 claude-haiku-5-5，effort medium，同时识别 1 页。"
    assert len((task / "results" / "progress.log").read_text(encoding="utf-8").splitlines()) == len(saved) + 3
