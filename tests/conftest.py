import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "samples"))

import make_sample  # noqa: E402

from bidbench import debuglog, ingest, recognition, state  # noqa: E402
from bidbench.executors import codex, simulated  # noqa: E402
from bidbench.store import Project  # noqa: E402


def _recognize(proj):
    task = recognition.make_task(proj, render=False)
    if task:
        simulated.run(task)
        res = recognition.import_results(proj, task / "results")
        assert not res.errors, res.errors


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """用户设置（最近项目、API 密钥）、排错日志和临时文件都放进测试自己的目录，不碰真实的用户目录和系统临时文件夹。"""
    monkeypatch.setenv("BIDBENCH_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tmp")))
    yield
    debuglog.close()  # 命令行测试打开的日志文件用完就关，临时目录才删得掉


class RealServiceCalled(BaseException):
    """测试调用了真的 Codex。继承 BaseException：不会被“这一批没分成、继续”之类的处理吞掉。"""


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch):
    """测试不联网、不占额度：没有 Claude 密钥，真的 Codex 一调用就报错。要用 AI 的测试传入假的客户端或 FakeCodex。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def refuse(cmd, prompt, timeout):
        raise RealServiceCalled("测试调用了真的 Codex，请传入假的 runner（FakeCodex）")
    monkeypatch.setattr(codex, "_subprocess_runner", refuse)


@pytest.fixture(scope="session")
def sample_zips(tmp_path_factory):
    return make_sample.build(tmp_path_factory.mktemp("zips"))


@pytest.fixture(scope="session")
def demo(tmp_path_factory, sample_zips):
    """按演示流程逐批收图、识别；第 2 批后锁定基线。"""
    proj = Project.init(tmp_path_factory.mktemp("proj") / "p", "示例综合体项目")
    results = []
    for i, (path, date) in enumerate(sample_zips):
        results.append(ingest.receive(proj, path, date))
        _recognize(proj)
        if i == 1:
            state.create_baseline(proj, "3层结构模型", ["结施-01", "结施-04", "建施-03"])
    proj.receive_results = results
    return proj


@pytest.fixture
def sheets_project(tmp_path):
    """把几张合成图纸（文件名 → PDF 内容）作为一批收进新项目，用模拟执行器识别。"""
    def build(files: dict, name: str = "合成项目"):
        folder = tmp_path / "图纸"
        folder.mkdir()
        for fn, data in files.items():
            (folder / fn).write_bytes(data)
        proj = Project.init(tmp_path / "p", name)
        ingest.receive(proj, folder, "2026-01-01")
        _recognize(proj)
        return proj
    return build


@pytest.fixture
def demo_copy(demo, tmp_path):
    """可写副本：做决定的测试不污染共享项目。"""
    dst = tmp_path / "copy"
    shutil.copytree(demo.root, dst)
    return Project.open(dst)
