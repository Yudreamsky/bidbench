"""整夜识别：没人看着也能一口气做完——读 DWG、分步识别、智能分类、算报告。

- 单个文件、单页出错：记下来接着做。读不出的 DWG 按文件名暂定；出不了图的页不放进任务包；识别失败的页，
  这一步做完再试 RETRY_ROUNDS 轮，还不行留给下次。
- 额度用完：停下来等，每隔 wait 分钟试一次，额度恢复后用同一个任务包接着识别。
- 截止时间（until）：到点不再开始新的页、不再等额度，正在识别的做完就停；照样分类、算报告，做好的部分能用。
  白天的 Codex 额度留给白天用。
- 中途断了（关机、Ctrl+C、程序出错）：再运行一次就接着做，读过的文件、已导入的识别结果不重做。
- 识别按分步识别的顺序来（重点图、建模主图、按需深入），一步的图片准备好就识别，最要紧的先做完。
- 过程一行一行写进项目 logs 文件夹的日志（带时间，写一行存一行），第二天打开就知道做到了哪；运行期间不让电脑自动睡眠。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import oplog, ops, recognition
from .store import Project

logger = logging.getLogger(__name__)

WAIT_MINUTES = 20  # 额度用完后隔多久再试
RETRY_ROUNDS = 1   # 一步识别完，失败的页再试几轮
FOLLOW_ROUNDS = 2  # 聚焦识别：识别出的索引又指到新的图，再识别几轮
EXECUTORS = ("codex", "claude")
FAILED = object()  # 某一步出错了（和“这一步没有要做的”区分开）


def deadline(hhmm: str, now: datetime | None = None) -> datetime:
    """“07:30” → 下一个 07:30（今天已经过了就是明天的）。"""
    now = now or datetime.now()
    try:
        t = datetime.strptime(hhmm.strip(), "%H:%M")
    except ValueError:
        raise ValueError(f"截止时间写成 时:分，比如 07:30（收到的是“{hhmm}”）") from None
    d = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return d if d > now else d + timedelta(days=1)


class _Log:
    """照常输出，同时一行一行追加进日志文件，每行带时间；写一行存一行，断电也只丢最后一行。"""

    def __init__(self, path: Path, out: Callable[[str], None]):
        self.path, self.out, self.lock = path, out, threading.Lock()

    def __call__(self, msg: str) -> None:
        self.out(msg)
        stamp = datetime.now().strftime("%m-%d %H:%M:%S")
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.writelines(f"[{stamp}] {ln}\n" for ln in (str(msg).splitlines() or [""]))
            except OSError:
                pass  # 写不进日志不影响识别


def _keep_awake(on: bool) -> None:
    """运行期间不让 Windows 自动睡眠（屏幕照常可以关）；结束或程序退出后自动恢复。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    except (AttributeError, OSError):
        pass


def run(proj: Project, executor: str = "codex", scope: str = "new", tier: str = "economy", model: str | None = None,
        stages: list[str] | None = None, until: datetime | None = None, wait: float = WAIT_MINUTES,
        workers: int | None = None, log: ops.Log = print, sleep=time.sleep, clock=datetime.now,
        runner=None, client=None, focus: recognition.Focus | None = None) -> dict:
    """整夜识别，返回各步的结果和日志文件的位置。focus 给了只识别聚焦要用的图：先智能分类认出专业，识别出的索引
    又指到新的图，再识别 FOLLOW_ROUNDS 轮。runner（假的 Codex）、client（假的 Claude）、sleep、clock 只在测试里换。"""
    if executor not in EXECUTORS:
        raise ValueError(f"整夜识别用 Codex 或 Claude 识别（收到的是 {executor}）")
    if scope not in recognition.SCOPES:
        raise ValueError(f"未知的识别范围 {scope}，可选：{'、'.join(recognition.SCOPES)}")
    if focus:
        recognition.focus_pages(proj, focus)  # 塔楼写错、楼层看不出：开始以前就说
    path = oplog.folder(proj) / f"整夜识别-{clock():%Y%m%d-%H%M%S}.log"
    say = _Log(path, log)
    n = _Night(proj, say, executor, scope, tier, model, until, wait, workers, sleep, clock, runner, client, focus)
    _keep_awake(True)
    try:
        say(f"整夜识别开始：项目「{proj.meta['name']}」，用 {ops.CLASSIFY_BY[executor]}，范围：{recognition.SCOPES[scope]}"
            + (f"，只识别聚焦 {focus.text()} 要用的图" if focus else "")
            + (f"，最晚做到 {until:%m-%d %H:%M}" if until else "，做完为止") + "。")
        say(f"日志：{path}")
        say("—— 读 DWG ——")
        n.out["dwg"] = n.value(n.step("读 DWG", lambda: ops.read_cad(proj, log=say)))
        if n.out["dwg"] and not n.out["dwg"]["files"] and not n.out["dwg"]["no_tool"]:
            say("DWG、DXF 都读过了。")
        if focus:  # 图号认不出专业的，靠智能分类按图名、文件夹认：先分好，才知道哪些是要识别的
            n.out["classify"] = n.classify()
        # 聚焦的先识别图纸目录建大地图（不按聚焦筛）：文件名只有图号的，靠目录上的图名才知道哪张是这一层的图
        steps = stages or [*([recognition.MAP_STAGE] if focus else []), *(k for k, _lb, _d in recognition.STAGES)]
        why = n.step("识别", lambda: n.recognize(steps))
        for i in range(FOLLOW_ROUNDS if focus else 0):
            more = n.left() if why is None else None
            if not more:
                break
            say(f"—— 识别出的索引指到新的图，还有 {more} 页：再识别一轮（第 {i + 1} 轮） ——")
            why = n.step("识别", lambda: n.recognize(steps))
        n.out["stopped"] = "识别出了意外的错，见上面" if why is FAILED else why
        n.out["left"] = n.left()
        n.out["done"] = n.value(n.step("统计识别了几页", n.done)) or 0
        n.out["classify"] = n.classify() or n.out["classify"]
        n.out["build"] = n.value(n.step("算报告", lambda: ops.build(proj, log=say)))
        n.summary()
    finally:
        _keep_awake(False)
    return n.out


class _Night:
    def __init__(self, proj, say, executor, scope, tier, model, until, wait, workers, sleep, clock, runner, client,
                 focus=None):
        self.proj, self.say, self.executor, self.scope, self.tier, self.model = proj, say, executor, scope, tier, model
        self.until, self.wait_minutes, self.workers, self.sleep, self.clock = until, wait, workers, sleep, clock
        self.runner, self.client, self.focus = runner, client, focus
        self.out = {"log": str(say.path), "dwg": None, "done": 0, "failed": 0, "skipped_pages": 0, "stopped": None,
                    "classify": None, "build": None, "left": None}
        self.plan = None  # 最后一次统计的还剩多少（给结尾的汇总用）
        self.tried: set[str] = set()  # 这次放进过任务包的页

    def done(self) -> int:
        """这次识别了几页：放进过任务包、现在不用再识别的页（中途停下续跑过、聚焦的图越跟越多也算得准）。"""
        still = {r["page_id"] for r in recognition.pick_pages(self.proj, self.scope, focus=self.focus)}
        return len(self.tried - still)

    def late(self) -> bool:
        return self.until is not None and self.clock() >= self.until

    def left(self) -> int | None:
        """范围内还要识别几页（不出图、不调用 AI，几千页也很快）；统计不了返回 None。"""
        plan = self.step("统计还剩多少", lambda: ops.plan_recognition(self.proj, self.scope, self.executor, self.tier,
                                                                      self.model, workers=self.workers,
                                                                      focus=self.focus))
        if plan is FAILED:
            return None
        self.plan = plan
        return plan["pages"] if plan else 0

    @staticmethod
    def value(r):
        return None if r is FAILED else r

    def step(self, name: str, fn):
        """做一步；出错记下来，返回 FAILED，后面几步照做。"""
        try:
            return fn()
        except ops.OpError as e:
            self.say(f"✗ {name}没有完成：{e}")
        except Exception as e:  # noqa: BLE001 - 一步出了意外的错：记下来，后面几步照做，再运行一次接着做
            logger.exception("整夜识别：%s出错", name)
            self.say(f"✗ {name}出了意外的错（{type(e).__name__}: {e}），后面几步照做；再运行一次会接着做。")
        return FAILED

    def wait(self, e: Exception) -> bool:
        """额度用完：过 wait 分钟再试；到截止时间前等不到下一次就不等了，返回 False。"""
        again = self.clock() + timedelta(minutes=self.wait_minutes)
        if self.until is not None and again > self.until:
            self.say(f"✗ {e}\n  截止时间 {self.until:%H:%M} 前等不到下一次重试，不再等。")
            return False
        self.say(f"⏸ {e}\n  {self.wait_minutes:g} 分钟后（{again:%H:%M}）再试。")
        self.sleep(self.wait_minutes * 60)
        return True

    def recognize(self, stages: list[str]) -> str | None:
        """一步一步识别；返回停下的原因，都做完了返回 None。"""
        for key in stages:
            label = recognition.STAGE_LABEL[key]
            if self.late():
                return f"到了截止时间 {self.until:%H:%M}，{label}和后面几步没有开始"
            self.say(f"—— {label} ——")
            focus = None if key == recognition.MAP_STAGE else self.focus  # 大地图：全部的图纸目录、发图单
            if self.executor == "codex":
                est = self.step(f"准备{label}", lambda: ops.prepare_codex(self.proj, self.scope, self.tier, self.model,
                                                                         workers=self.workers, stages=[key], focus=focus))
            else:
                est = self.step(f"准备{label}", lambda: ops.prepare_claude(self.proj, self.scope, self.tier, self.model,
                                                                          stages=[key], focus=focus))
            if est is FAILED:
                continue
            if est is None:
                self.say(f"{label}：没有要识别的页。")
                continue
            task = Path(est["task"])
            self.say(ops.estimate_text(est))
            self._report_skipped(task)
            why = self._run_task(task, label)
            if why:
                return why
        return None

    def _report_skipped(self, task: Path) -> None:
        manifest = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))
        self.tried |= {it["page_id"] for it in manifest["pages"]}
        skipped = manifest.get("skipped") or []
        self.out["skipped_pages"] += len(skipped)
        for s in skipped[:10]:
            self.say(f"  ✗ 出不了图，没放进任务包：{s['file_path']}——{s['error']}")
        if len(skipped) > 10:
            self.say(f"  ……另有 {len(skipped) - 10} 页，见任务包的 manifest_in.json")

    def _run_task(self, task: Path, label: str) -> str | None:
        """识别一个任务包：额度用完就等着续跑，失败的页再试几轮。返回停下整夜识别的原因。"""
        rounds = 0
        while True:
            along = ops.ClassifyAlong(self.proj, self.executor, self.model, log=self.say)
            try:
                if self.executor == "codex":
                    r = ops.recognize_codex(self.proj, task, self.tier, self.model, workers=self.workers, log=self.say,
                                            runner=self.runner, on_import=along, stop=self.late)
                else:
                    r = ops.recognize_claude(self.proj, task, self.tier, self.model, workers=self.workers or 4,
                                             log=self.say, client=self.client, on_import=along, stop=self.late)
            except ops.LimitReached as e:
                along.wait()
                if self.wait(e):
                    continue
                return f"额度用完，到截止时间还没恢复：{e}"
            except ops.OpError as e:
                along.wait()
                self.say(f"✗ 识别停下了：{e}")
                return str(e)
            except Exception as e:  # noqa: BLE001 - 意外的错：这一步先停，下一步照做
                along.wait()
                logger.exception("整夜识别：%s出错", label)
                self.say(f"✗ {label}出了意外的错（{type(e).__name__}: {e}），这一步先停，接着做下一步。")
                return None
            along.wait()
            if r["later"]:
                self.out["failed"] += r["failed"]
                return f"到了截止时间 {self.until:%H:%M}，{label}还有 {r['later']} 页没开始"
            if r["failed"] and rounds < RETRY_ROUNDS and not self.late():
                rounds += 1
                self.say(f"{label}有 {r['failed']} 页没识别成，再试一轮。")
                continue
            self.out["failed"] += r["failed"]
            return None

    def classify(self) -> dict | None:
        """智能分类（只发图号、图名、文件夹名，几分钟）；额度用完同样等着。"""
        self.say("—— 智能分类 ——")
        while True:
            try:
                return ops.classify(self.proj, self.executor, self.model if self.executor == "codex" else None,
                                    log=self.say, runner=self.runner, client=self.client)
            except ops.LimitReached as e:
                if not self.wait(e):
                    return None
            except ops.OpError as e:
                self.say(f"✗ 智能分类没有完成：{e}。稍后可以再分（命令行：bidbench classify）。")
                return None
            except Exception as e:  # noqa: BLE001 - 分不成不影响识别结果
                logger.exception("整夜识别：智能分类出错")
                self.say(f"✗ 智能分类出了意外的错（{type(e).__name__}: {e}）。稍后可以再分（命令行：bidbench classify）。")
                return None

    def summary(self) -> None:
        o = self.out
        self.say("—— 整夜识别结束 ——")
        d = o["dwg"]
        if d and d["files"]:
            self.say(f"读 DWG、DXF：{d['files']} 个文件，认出 {d['sheets']} 张图"
                     + (f"，{d['failed']} 个读不出（按文件名暂定）" if d["failed"] else "") + "。")
        self.say(f"这次识别 {o['done']} 页" + (f"，{o['failed']} 页没识别成（下次运行再试）" if o["failed"] else "")
                 + (f"，{o['skipped_pages']} 页出不了图" if o["skipped_pages"] else "") + "。")
        if o["stopped"]:
            self.say(f"识别停在：{o['stopped']}。再运行一次整夜识别会接着做。")
        if o["left"] == 0:
            m = self.value(self.step("统计", lambda: recognition.stage_plan(self.proj, self.scope, self.focus)["map"]))
            self.say("范围内的页都识别过了。" + (ops.map_text(m) if m else ""))
        elif self.plan:
            self.say(ops.stages_text(self.plan))
        self.say(f"报告在 {self.proj.root / 'reports'}，日志在 {o['log']}")
