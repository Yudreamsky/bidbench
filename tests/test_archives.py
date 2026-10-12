"""rar / 7z 解包：用系统自带或已安装的工具（bsdtar、7-Zip、unrar）。"""

import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from bidbench import extractors, ingest
from bidbench.store import Project

DATA = Path(__file__).parent / "data" / "rar"
ANY_TOOL = bool(extractors.find_bsdtar() or extractors.find_7z() or extractors.find_unrar())
needs_tool = pytest.mark.skipif(not ANY_TOOL, reason="没有可用的解压工具（bsdtar / 7-Zip / unrar）")


def paths(proj, seq):
    with proj.connect() as con:
        return [r["path"] for r in con.execute("SELECT path FROM occurrences WHERE batch_seq=?", (seq,))]


@pytest.fixture
def proj(tmp_path):
    return Project.init(tmp_path / "p", "解包测试")


@needs_tool
def test_rar_as_batch_and_nested(proj, tmp_path):
    r = ingest.receive(proj, DATA / "rar5-subdirs.rar", "2026-01-01")
    assert not r.issues and r.files == 4
    got = paths(proj, 1)
    assert any(p.endswith("rar5-subdirs.rar / sub / dir1 / file1.txt") for p in got)
    assert any("üȵĩöḋè" in p for p in got)  # 非 ASCII 文件名完整保留

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("投标图纸/补充/图纸.rar", (DATA / "rar3-subdirs.rar").read_bytes())
        z.writestr("投标图纸/补充/名称.rar", (DATA / "unicode2.rar").read_bytes())
    outer = tmp_path / "外层.zip"
    outer.write_bytes(buf.getvalue())
    r2 = ingest.receive(proj, outer, "2026-01-02")
    assert not r2.issues, r2.issues
    assert len(r2.nested_archives) == 2
    got = paths(proj, 2)
    assert any(p.endswith("图纸.rar / sub / with space / long fn.txt") for p in got)
    assert any("уииоотивл.txt" in p for p in got)


@needs_tool
@pytest.mark.parametrize("name", ["rar5-hpsw.rar", "rar5-psw.rar"])
def test_encrypted_rar_is_reported_not_half_extracted(proj, name):
    r = ingest.receive(proj, DATA / name, "2026-01-01")
    assert r.files == 0  # 不能把解了一半的乱码文件登记进来
    assert len(r.issues) == 1 and r.issues[0]["kind"] in ("encrypted", "corrupt")


def _can_decrypt_rar() -> bool:
    if extractors.find_unrar():
        return True
    tool = extractors.find_7z()
    if not tool:
        return False
    out = subprocess.run([tool, "i"], capture_output=True, text=True).stdout
    return "Rar5" in out or "RAR5" in out


@pytest.mark.skipif(not _can_decrypt_rar(), reason="需要 unrar 或带 rar 解码的 7-Zip 才能解加密 rar")
def test_encrypted_rar_with_password(proj):
    r = ingest.receive(proj, DATA / "rar5-hpsw.rar", "2026-01-01", passwords=["password"])
    if r.issues and "Unsupported" in r.issues[0]["detail"]:
        pytest.skip("本机的 7-Zip 不支持这种加密 rar 的压缩方法：" + r.issues[0]["detail"])
    assert not r.issues and r.files == 2


@needs_tool
def test_evil_symlink_cannot_escape(proj, tmp_path):
    before = set(tmp_path.parent.iterdir())
    r = ingest.receive(proj, DATA / "rar5-evil-symlink-traversal.rar", "2026-01-01")
    assert not any("pwned" in p for p in paths(proj, 1))
    assert r.issues and r.issues[0]["kind"] in ("symlink", "corrupt")
    assert set(tmp_path.parent.iterdir()) == before  # 没有写到压缩包以外


def test_no_tool_gives_install_hint(proj, monkeypatch):
    for fn in ("find_unrar", "find_7z", "find_bsdtar"):
        monkeypatch.setattr(extractors, fn, lambda: None)
    r = ingest.receive(proj, DATA / "rar5-subdirs.rar", "2026-01-01")
    assert r.files == 0 and r.issues[0]["kind"] == "no_tool"
    assert "Windows 10" in r.issues[0]["detail"] and "libarchive-tools" in r.issues[0]["detail"]


def _make_7z(tmp_path) -> Path:
    py7zr = pytest.importorskip("py7zr")
    src = tmp_path / "src"
    (src / "结构").mkdir(parents=True)
    (src / "结构" / "说明.txt").write_text("结施-01 结构设计总说明", encoding="utf-8")
    out = tmp_path / "补充图纸.7z"
    with py7zr.SevenZipFile(out, "w") as z:
        z.writeall(src, arcname="补充图纸")
    return out


def test_7z_with_py7zr(proj, tmp_path):
    r = ingest.receive(proj, _make_7z(tmp_path), "2026-01-01")
    assert not r.issues and r.files == 1
    assert any(p.endswith("补充图纸 / 结构 / 说明.txt") for p in paths(proj, 1))


@needs_tool
def test_7z_without_py7zr_uses_system_tool(proj, tmp_path, monkeypatch):
    archive = _make_7z(tmp_path)
    monkeypatch.setitem(sys.modules, "py7zr", None)  # 模拟没装 py7zr
    r = ingest.receive(proj, archive, "2026-01-01")
    assert not r.issues and r.files == 1
