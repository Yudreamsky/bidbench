"""整夜识别：一条命令读 DWG、分步识别、智能分类、算报告；额度用完等着续跑，到点停下，过程写进日志。
用假的 Codex 和假的时钟，不联网、不占额度、不真的等。"""

import json
from datetime import datetime, timedelta

import pytest
from conftest import _recognize
from test_claude_executor import FakeClient, mimic
from test_classify import ai, pdf
from test_codex_executor import FakeCodex

from bidbench import cli, ingest, oplog, ops, overnight, recognition, state
from bidbench.executors import claude, codex
from bidbench.store import Project

START = datetime(2026, 10, 21, 22, 0)


@pytest.fixture
def night(sheets_project):
    return sheets_project({
        "建施-00 图纸目录.pdf": pdf("建施-00", "图纸目录", "建施-01 建筑设计总说明", "建施-02 一层平面图"),
        "建施-01 建筑设计总说明.pdf": pdf("建施-01", "建筑设计总说明", "地上 20 层，地下 2 层"),
        "建施-02 一层平面图.pdf": pdf("建施-02", "一层平面图", "1F 层高 5.10 m"),
        "建施-03 二层平面图.pdf": pdf("建施-03", "二层平面图", "2F 层高 4.50 m"),
        "建施-05 1-1剖面图.pdf": pdf("建施-05", "1-1剖面图"),
        "结施-03 三层梁配筋图.pdf": pdf("结施-03", "三层梁配筋图", "KL1 截面 300×650"),
    })


class Clock:
    """假时钟：每问一次 Codex 走 step 分钟。"""

    def __init__(self, step: int = 0):
        self.now, self.step = START, timedelta(minutes=step)

    def __call__(self):
        return self.now

    def tick(self):
        self.now += self.step


def _codex(answer=ai, clock: Clock | None = None):
    def wrapped(p, n):
        if clock:
            clock.tick()
        return answer(p, n)
    return FakeCodex(wrapped)


def _no_sleep(seconds):
    pytest.fail(f"不该等（{seconds} 秒）")


def _log_lines(r) -> list[str]:
    with open(r["log"], encoding="utf-8") as f:
        return f.read().splitlines()


def test_deadline():
    assert overnight.deadline("07:30", START) == datetime(2026, 10, 22, 7, 30)  # 晚上开始：第二天早上
    assert overnight.deadline("23:15", START) == datetime(2026, 10, 21, 23, 15)
    with pytest.raises(ValueError, match="时:分"):
        overnight.deadline("七点半", START)


def test_usage_limit_is_its_own_kind_of_stop():
    assert isinstance(codex.fatal_error("You've hit your usage limit. Try again in 3 hours.", "m"), claude.LimitError)
    e = codex.fatal_error("401 Unauthorized", "m")
    assert isinstance(e, claude.FatalError) and not isinstance(e, claude.LimitError)
    assert codex.fatal_error("stream disconnected", "m") is None  # 只算这一页失败
    assert "额度" in codex.classify("rate limit reached", "m")


def test_overnight_does_every_step_and_logs_each_line(night):
    fake, lines = _codex(), []
    r = overnight.run(night, runner=fake, log=lines.append, sleep=_no_sleep, clock=Clock(), workers=1)
    assert (r["done"], r["failed"], r["stopped"], r["left"]) == (6, 0, None, 0)
    assert r["build"]["as_of"] == 1
    log = _log_lines(r)
    assert log[0].startswith("[") and "整夜识别开始" in log[0] and "做完为止" in log[0]
    assert any("—— 重点图 ——" in x for x in log) and any("—— 智能分类 ——" in x for x in log)
    assert any("整夜识别结束" in x for x in log) and any("范围内的页都识别过了" in x for x in log)
    assert len(log) >= len(lines)  # 屏幕上的每一行都进了日志
    st = state.build(night)
    assert st["classification"]["sheets_done"] == r["classify"]["sheets"] == len(st["sheets"]) >= 6
    assert not ops.plan_recognition(night, "new", "codex")
    # 再运行一次：都做过了，什么也不重做
    again = overnight.run(night, runner=_codex(), log=lambda s: None, sleep=_no_sleep, clock=Clock(), workers=1)
    assert again["done"] == 0 and again["left"] == 0


def test_waits_out_the_usage_limit_and_carries_on(night):
    def limited(p, n):
        return (None, "You've hit your usage limit. Try again in 20 minutes.") if n == 2 else ai(p, n)
    slept = []
    r = overnight.run(night, runner=_codex(limited), log=lambda s: None, sleep=slept.append, clock=Clock(), workers=1,
                      wait=15)
    assert slept == [15 * 60]
    assert (r["done"], r["failed"], r["stopped"], r["left"]) == (6, 0, None, 0)
    assert any("⏸" in x and "额度" in x for x in _log_lines(r))


def test_stops_at_the_deadline_and_still_classifies_and_reports(night):
    clock = Clock(step=30)
    r = overnight.run(night, runner=_codex(clock=clock), log=lambda s: None, sleep=_no_sleep, clock=clock,
                      until=START + timedelta(hours=1), workers=1)
    assert r["done"] == 2 and "截止时间 23:00" in r["stopped"]
    assert r["left"] == 4 and r["build"] is not None and r["classify"] is not None
    log = _log_lines(r)
    assert any("最晚做到 10-21 23:00" in x for x in log) and any("再运行一次整夜识别会接着做" in x for x in log)
    # 第二天晚上再跑：接着识别剩下的
    r2 = overnight.run(night, runner=_codex(), log=lambda s: None, sleep=_no_sleep, clock=Clock(), workers=1)
    assert (r2["done"], r2["left"]) == (4, 0)


def test_limit_that_outlasts_the_deadline_stops_waiting(night):
    clock = Clock()

    def always_limited(p, n):
        return None, "You've hit your usage limit."
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += timedelta(seconds=seconds)
    r = overnight.run(night, runner=_codex(always_limited), log=lambda s: None, sleep=sleep, clock=clock,
                      until=START + timedelta(minutes=50), wait=20, workers=1)
    assert slept == [1200, 1200]  # 22:20、22:40 各试一次，23:00 前等不到下一次
    assert r["done"] == 0 and "额度用完" in r["stopped"] and r["classify"] is None and r["build"] is not None


def test_codex_not_logged_in_stops_recognition_but_reports(night):
    def logged_out(p, n):
        return None, "401 Unauthorized"
    r = overnight.run(night, runner=_codex(logged_out), log=lambda s: None, sleep=_no_sleep, clock=Clock(), workers=1)
    assert "codex login" in r["stopped"] and r["done"] == 0
    assert r["classify"] is None and r["build"] is not None and r["left"] == 6
    assert any("智能分类没有完成" in x for x in _log_lines(r))


def test_cli_overnight_is_logged(night, monkeypatch, capsys):
    """命令行：没有 Codex 时也照样读 DWG、出报告，日志和操作日志都有。"""
    monkeypatch.setattr(codex, "find_codex", lambda refresh=False: None)
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: {"ready": False, "hint": "没有找到 Codex"})
    cli.main(["-p", str(night.root), "overnight", "--until", "07:30"])
    out = capsys.readouterr().out
    assert "整夜识别开始" in out and "没有找到 Codex" in out and "整夜识别结束" in out
    entry = oplog.read(night)[0]
    assert entry["title"] == "整夜识别（Codex 经济档）" and entry["status"] == "done"
    assert list((night.root / oplog.FOLDER).glob("整夜识别-*.log"))
    with pytest.raises(SystemExit, match="时:分"):
        cli.main(["-p", str(night.root), "overnight", "--until", "7点"])


def test_page_that_cannot_be_rendered_is_left_out_of_the_task(night, monkeypatch):
    """一页出不了图（文件坏了、被删了）：不放进任务包，记在清单里，别的页照常识别。"""
    real = recognition.pdfpages.render_page
    broken = []

    def render(pdf_path, page_index, out, dpi=150, clip_frac=None):
        if not broken:
            broken.append(out.stem)
            raise RuntimeError("cannot render")
        return real(pdf_path, page_index, out, dpi, clip_frac)
    monkeypatch.setattr(recognition.pdfpages, "render_page", render)
    seen = []
    task = recognition.make_task(night, progress=lambda n, t: seen.append((n, t)))
    manifest = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))
    assert [s["page_id"] for s in manifest["skipped"]] == broken and "RuntimeError" in manifest["skipped"][0]["error"]
    assert len(manifest["pages"]) == 5 and seen[-1] == (6, 6)
    monkeypatch.setattr(recognition.pdfpages, "render_page", real)
    r = overnight.run(night, runner=_codex(), log=lambda s: None, sleep=_no_sleep, clock=Clock(), workers=1)
    assert r["done"] == 6 and r["skipped_pages"] == 0  # 下次准备任务包时这一页好了，照常识别


def test_overnight_with_claude_api(night):
    """用 Claude API 识别也一样：分步识别、到点停、分类、出报告。"""
    clock = Clock()

    def answer(params, n):
        first = params["messages"][0]["content"][0]["text"]
        if "下面每行是" in first:  # 智能分类
            return ai(first, n)[0], "end_turn"
        clock.now += timedelta(minutes=30)
        return mimic(params, n)
    r = overnight.run(night, "claude", client=FakeClient(answer), log=lambda s: None, sleep=_no_sleep, clock=clock,
                      until=START + timedelta(hours=1), workers=1)
    assert r["done"] == 2 and "截止时间" in r["stopped"] and r["classify"]["sheets"] >= 6 and r["left"] == 4
    r = overnight.run(night, "claude", client=FakeClient(answer), log=lambda s: None, sleep=_no_sleep, clock=Clock(),
                      workers=1)
    assert (r["done"], r["left"], r["stopped"]) == (4, 0, None)


def test_overnight_only_the_disciplines_asked_for(night):
    """只要结构的图（图号认不出专业的先智能分类）：别的专业的页不识别，也不算在“还剩多少”里，下次再识别。"""
    fake, lines = _codex(), []
    r = overnight.run(night, runner=fake, log=lines.append, sleep=_no_sleep, clock=Clock(), workers=1,
                      focus=recognition.Focus(("结构",)))
    assert (r["done"], r["failed"], r["stopped"], r["left"]) == (1, 0, None, 0)
    left = recognition.pick_pages(night, "new")
    assert len(left) == 5 and all("建施" in x["first_path"] for x in left)
    log = "\n".join(_log_lines(r))
    assert "只识别聚焦 结构 要用的图" in log and log.index("—— 智能分类 ——") < log.index("—— 建模主图 ——")
    assert cli._disciplines("建筑，结构") == ("建筑", "结构")
    with pytest.raises(SystemExit, match="不认识的专业 机房"):
        cli._disciplines("建筑,机房")


@pytest.fixture
def towers(tmp_path):
    """两栋楼、各层的图：聚焦 T1 九层的建筑、结构。"""
    files = {
        "T1/建施-01 建筑设计总说明.pdf": pdf("建施-01", "建筑设计总说明", "地上 20 层"),
        "T1/建施-03 三层平面图.pdf": pdf("建施-03", "三层平面图"),
        "T1/建施-09 九层平面图.pdf": pdf("建施-09", "九层平面图", "楼梯详见建施-20"),
        "T1/建施-10 九至十二层平面图.pdf": pdf("建施-10", "九至十二层平面图"),
        "T1/建施-05 1-1剖面图.pdf": pdf("建施-05", "1-1剖面图"),
        "T1/建施-20 楼梯详图.pdf": pdf("建施-20", "楼梯详图", "栏杆详见建施-22"),
        "T1/建施-21 卫生间详图.pdf": pdf("建施-21", "卫生间详图"),
        "T1/建施-22 栏杆详图.pdf": pdf("建施-22", "栏杆详图"),
        "T1/结施-09 九层梁配筋图.pdf": pdf("结施-09", "九层梁配筋图", "KL1 截面 300×650"),
        "T1/电施-09 九层照明平面图.pdf": pdf("电施-09", "九层照明平面图"),
        "T2/建施-09 九层平面图.pdf": pdf("建施-09", "九层平面图"),
    }
    for rel, data in files.items():
        (tmp_path / "图纸" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "图纸" / rel).write_bytes(data)
    proj = Project.init(tmp_path / "p", "双塔")
    ingest.receive(proj, tmp_path / "图纸", "2026-01-01")
    return proj


def _focus_names(proj, focus) -> set[str]:
    keep = recognition.focus_pages(proj, focus)
    with proj.connect() as con:
        return {r["path"].split(" / ")[-1].split(" ")[0] if not r["path"].split(" / ")[-2].startswith("T2")
                else "T2·" + r["path"].split(" / ")[-1].split(" ")[0]
                for r in con.execute("SELECT o.path, p.page_id FROM pages p JOIN occurrences o ON o.sha256 = p.file_sha256")
                if r["page_id"] in keep}


def test_focus_on_one_floor_of_one_tower(towers):
    """聚焦 T1 九层的建筑、结构：九层的图、标准层、说明、剖面要；九层平面上索引到的楼梯详图、楼梯详图上再索引到的栏杆详图，
    识别出索引以后跟着要；别的楼层、没被索引的详图、机电、T2 都不要。"""
    focus = recognition.Focus(("建筑", "结构"), "T1", "9F")
    first = {"建施-01", "建施-09", "建施-10", "建施-05", "结施-09"}
    assert _focus_names(towers, focus) == first  # 还没看图识别：不知道索引了哪些详图
    _recognize(towers)  # 模拟执行器读出文字里的索引
    assert _focus_names(towers, focus) == first | {"建施-20", "建施-22"}
    with pytest.raises(ValueError, match="没有“T3”这栋楼"):
        recognition.focus_pages(towers, recognition.Focus(tower="T3", floor="9F"))
    with pytest.raises(ValueError, match="看不出“九楼半”是哪一层"):
        recognition.focus_pages(towers, recognition.Focus(floor="九楼半"))
    assert recognition.focus_floors("LEVEL 09", 1) == {10} and recognition.focus_floors("LEVEL 09", 0) == {9}
    assert recognition.focus_floors("9", 1) == {9} == recognition.focus_floors("9F", 0)
