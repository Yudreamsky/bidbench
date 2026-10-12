import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "samples"))

import make_sample  # noqa: E402

from bidbench import ingest, recognition, state  # noqa: E402
from bidbench.executors import simulated  # noqa: E402
from bidbench.store import Project  # noqa: E402


def _recognize(proj):
    task = recognition.make_task(proj, render=False)
    if task:
        simulated.run(task)
        res = recognition.import_results(proj, task / "results")
        assert not res.errors, res.errors


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """用户设置（最近项目、API 密钥）写到临时目录，不碰真实的用户目录。"""
    monkeypatch.setenv("BIDBENCH_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))


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
def demo_copy(demo, tmp_path):
    """可写副本：做决定的测试不污染共享项目。"""
    dst = tmp_path / "copy"
    shutil.copytree(demo.root, dst)
    return Project.open(dst)
