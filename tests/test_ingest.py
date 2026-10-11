import zipfile

import pytest

from bidbench import ingest
from bidbench.store import Project


def test_batches_and_dedup(demo):
    r1, r2, r3, r4 = demo.receive_results
    assert r1.files == 14 and r1.new_pages == 15  # 合订本 2 页
    # 整套重发：15 个文件里大部分与批次1 完全相同，只新增 8 页
    assert r2.files == 15 and r2.by_kind == {"pdf": 14, "office": 1}
    assert r2.new_pages == 8
    with demo.connect() as con:
        # docx 本身是 zip 格式，不能被当成压缩包展开
        kinds = {r["kind"] for r in con.execute("SELECT kind FROM files WHERE ext='.docx'")}
        assert kinds == {"office"}


def test_nested_and_encrypted(demo):
    r3 = demo.receive_results[2]
    assert any(p.endswith("补充图纸.zip") for p in r3.nested_archives)
    enc = [i for i in r3.issues if i["kind"] == "encrypted"]
    assert len(enc) == 1 and enc[0]["path"].endswith("修改.zip")
    with demo.connect() as con:
        paths = [r["path"] for r in con.execute("SELECT path FROM occurrences WHERE batch_seq=3")]
    assert any(p.endswith("补充图纸.zip / 结施-03 剪力墙配筋图.pdf") for p in paths)


def test_gbk_file_names_are_decoded(demo):
    with demo.connect() as con:
        paths = [r["path"] for r in con.execute("SELECT path FROM occurrences WHERE batch_seq=4")]
    assert any("电施-01 照明平面图.pdf" in p for p in paths), paths


def test_same_archive_twice_is_refused(demo_copy, sample_zips):
    with pytest.raises(ValueError):
        ingest.receive(demo_copy, sample_zips[0][0], "2026-07-03")


def test_corrupt_and_unsafe_members(tmp_path):
    proj = Project.init(tmp_path / "p", "t")
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../escape/坏图.pdf", b"%PDF-1.4 not really a pdf")
        zf.writestr("说明.txt", "hello")
    r = ingest.receive(proj, z, "2026-01-01")
    assert any(i["kind"] == "corrupt" for i in r.issues)
    with proj.connect() as con:
        paths = [row["path"] for row in con.execute("SELECT path FROM occurrences")]
    assert all(".." not in p for p in paths)
    assert not (tmp_path / "escape").exists()
