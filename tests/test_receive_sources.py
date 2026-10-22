"""收图来源：文件夹、同时收到的几个文件；读不出来和被杀毒软件拦下的文件；连文件名都加密的 7z；找解压工具。

杀毒软件拦截都是模拟的（让读文件报错、或解压后删掉文件），不写入任何真实病毒特征。
"""

import errno
import io
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

import make_sample
from bidbench import cli, extractors, ingest, ops, recognition, reports, risk, state
from bidbench.executors import simulated
from bidbench.store import Project, add_decision


def _recognize(proj):
    task = recognition.make_task(proj, render=False)
    if task:
        simulated.run(task)
        assert not recognition.import_results(proj, task / "results").errors


def _paths(proj, seq):
    with proj.connect() as con:
        return [r["path"] for r in con.execute("SELECT path FROM occurrences WHERE batch_seq=? ORDER BY id", (seq,))]


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()


def _pdf(no, title, rev="A"):
    return make_sample.sheet_pdf(no, title, rev, "2026-01-01", [], [(rev, "2026-01-01", "出图")])


@pytest.fixture
def proj(tmp_path):
    return Project.init(tmp_path / "p", "收图测试")


# ---------------- 文件夹 ----------------

def test_folder_is_one_batch(proj, tmp_path):
    folder, when = make_sample.build_folder(tmp_path / "in")
    r = ingest.receive(proj, folder, when)
    assert r.source == "folder" and r.name == "1020补充" and not r.issues
    assert r.files == 4 and r.by_kind == {"pdf": 4}
    assert sorted(r.skipped) == ["~$招标说明.docx", "建筑/Thumbs.db"]  # 系统文件不收
    root = "批次#1 / 1020补充（文件夹）"
    got = _paths(proj, 1)
    assert f"{root} / 建筑 / 建施-09 楼梯大样.pdf" in got
    assert f"{root} / 电气.zip / 补充 / 应急照明.zip / 电施-03 应急照明平面图.pdf" in got
    # 文件夹里直接放的压缩包相当于外层包；它里面再套的才是内层
    assert r.nested_archives == [f"{root} / 电气.zip / 补充 / 应急照明.zip"]
    with proj.connect() as con:
        b = dict(con.execute("SELECT * FROM batches").fetchone())
    assert b["archive_name"] == "1020补充（文件夹）"
    manifest = proj.root / b["stored_path"]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["kind"] == "folder" and {f["path"] for f in data["files"]} >= {"电气.zip", "建筑/建施-12 幕墙节点详图.pdf"}
    assert not os.stat(manifest).st_mode & stat.S_IWUSR  # 清单只读


def test_folder_batch_after_the_demo(demo_copy, tmp_path):
    folder, when = make_sample.build_folder(tmp_path / "in")
    ops.receive(demo_copy, folder, when, av=False, log=lambda m: None)
    _recognize(demo_copy)
    st = state.build(demo_copy)
    b5 = st["batches"][-1]
    assert b5["archive"] == "1020补充（文件夹）" and b5["kinds"] == ["补充图纸"]
    sheets = {s["no"]: s for s in st["sheets"]}
    assert {"建施-09", "建施-12"} <= set(st["diff"]["resolved_missing"])  # 文件夹补来的图核销了缺图
    assert not sheets["电施-02"]["inner"] and sheets["电施-03"]["inner"]
    assert "来源 1020补充（文件夹）" in reports.diff_report(st)


def test_same_folder_twice_and_changed_folder(proj, tmp_path):
    folder = tmp_path / "共享盘图纸"
    (folder / "建筑").mkdir(parents=True)
    for i in range(1, 6):
        (folder / "建筑" / f"建施-0{i}.pdf").write_bytes(_pdf(f"建施-0{i}", f"第{i}张"))
    ingest.receive(proj, folder, "2026-01-01")
    with pytest.raises(ValueError, match="完全一样"):
        ingest.receive(proj, folder, "2026-01-02")
    # 设计院在同一个文件夹里改了图：再收一次算新批次，按整套重发判断，不见了的图要问作废还是漏发
    (folder / "建筑" / "建施-05.pdf").unlink()
    (folder / "建筑" / "建施-01.pdf").write_bytes(_pdf("建施-01", "第1张", "B"))
    r = ingest.receive(proj, folder, "2026-01-02")
    assert r.seq == 2 and r.new_pages == 1
    _recognize(proj)
    st = state.build(proj)
    assert st["batches"][-1]["kinds"] == ["整套重发"]
    assert any(i["kind"] == "作废还是漏发" and i["title"].startswith("建施-05") for i in st["issues"])


def test_project_folder_is_never_received(tmp_path):
    outer = tmp_path / "投标"
    proj = Project.init(outer / "bidbench项目", "t")
    (outer / "图纸").mkdir()
    (outer / "图纸" / "建施-01.pdf").write_bytes(_pdf("建施-01", "总说明"))
    r = ingest.receive(proj, outer, "2026-01-01")  # 项目目录在收图文件夹里：跳过项目自己的数据
    assert r.files == 1 and all("bidbench项目" not in p for p in _paths(proj, 1))
    with pytest.raises(ValueError, match="项目目录里面"):
        ingest.receive(proj, proj.root / "archives", "2026-01-02")


def test_unreadable_file_in_folder_is_reported(proj, tmp_path, monkeypatch):
    folder = tmp_path / "图纸"
    folder.mkdir()
    (folder / "建施-01.pdf").write_bytes(_pdf("建施-01", "总说明"))
    (folder / "正在编辑.xlsx").write_bytes(b"PK\x03\x04 occupied")
    real = ingest.sha256_file

    def locked(p):
        if str(p).endswith("正在编辑.xlsx"):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(p)
    monkeypatch.setattr(ingest, "sha256_file", locked)
    r = ingest.receive(proj, folder, "2026-01-01")
    assert r.files == 1
    assert [(i["kind"], i["path"].split(" / ")[-1]) for i in r.issues] == [("unreadable", "正在编辑.xlsx")]
    assert "占用" in r.issues[0]["detail"]
    st = state.build(proj, with_diff=False)
    assert any(i["kind"] == "解包问题" and "读不出来" in i["title"] for i in st["issues"])
    assert st["security"]["files"] == []  # 被占用不是安全问题


def test_too_many_or_empty(proj, tmp_path, monkeypatch):
    (tmp_path / "空").mkdir()
    (tmp_path / "空" / "Thumbs.db").write_bytes(b"x")
    with pytest.raises(ValueError, match="没有可收的文件"):
        ingest.receive(proj, tmp_path / "空", "2026-01-01")
    monkeypatch.setattr(ingest, "MAX_SOURCE_FILES", 1)
    (tmp_path / "多").mkdir()
    for n in "ab":
        (tmp_path / "多" / f"{n}.txt").write_text(n)
    with pytest.raises(ValueError, match="请确认选对了文件夹"):
        ingest.receive(proj, tmp_path / "多", "2026-01-01")


def _tree(top: Path, n_dirs: int, per_dir: int, size: int = 10) -> Path:
    for d in range(n_dirs):
        (top / f"d{d}").mkdir(parents=True)
        for f in range(per_dir):
            (top / f"d{d}" / f"f{f:02d}.txt").write_bytes(b"x" * size)
    return top


def test_too_big_folder_stops_counting_early(proj, tmp_path, monkeypatch):
    """选到了整个共享盘：数到超过上限马上停，不用把整个文件夹列完才说收不下。"""
    top = _tree(tmp_path / "共享盘", 3, 10)
    listed = []
    real = os.scandir

    def scandir(p):
        listed.append(p)
        return real(p)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(ingest, "MAX_SOURCE_FILES", 5)
    with pytest.raises(ValueError, match="数到第 6 个文件就停下了.*请确认选对了文件夹"):
        ingest.receive(proj, top, "2026-01-01")
    assert len(listed) == 2  # 只列了最上一层和第一个子文件夹
    monkeypatch.setattr(ingest, "MAX_SOURCE_FILES", 100)
    monkeypatch.setattr(ingest, "MAX_TOTAL_BYTES", 25)
    with pytest.raises(ValueError, match="超过 0 GB（数到第 3 个文件就停下了）"):
        ingest.receive(proj, top, "2026-01-01")
    assert not proj.connect().execute("SELECT COUNT(*) FROM batches").fetchone()[0]


def test_folder_receive_reports_progress(proj, tmp_path, monkeypatch):
    """网络文件夹列得慢：边列边报找到几个，再报复制、登记，看得出没有卡住。"""
    monkeypatch.setattr(ingest, "LIST_NOTE_EVERY", 2)
    top = _tree(tmp_path / "图纸", 1, 5)
    lines = []
    ops.receive(proj, top, "2026-01-01", av=False, log=lines.append)
    assert lines[0] == f"列出文件夹里的文件：{top.resolve()}"
    assert [ln.split("（")[0] for ln in lines if "已找到" in ln] == ["  正在列出文件：已找到 2 个", "  正在列出文件：已找到 4 个"]
    assert any("5 个文件，共 0.00 GB；复制进项目" in ln for ln in lines) and any("登记文件" in ln for ln in lines)


def test_each_source_file_is_read_once(proj, tmp_path, monkeypatch):
    """来源（比如网络文件夹）里的每个文件只读一遍：复制进项目，认类型、查风险、算指纹、读 PDF 都读项目里那份。"""
    top = tmp_path / "网络盘"
    top.mkdir()
    (top / "建施-01.pdf").write_bytes(_pdf("建施-01", "总说明"))
    (top / "结构.zip").write_bytes(_zip({"结施-01.pdf": _pdf("结施-01", "总说明")}))
    (top / "说明.txt").write_text("说明")
    src = str(top.resolve()).lower()
    copied, touched = [], []
    real_copy, real_sha, real_head, real_type = shutil.copyfile, ingest.sha256_file, risk.read_head, ingest._archive_type

    def copy(a, b, **kw):
        if src in str(a).lower():
            copied.append(Path(a).name)
        return real_copy(a, b, **kw)

    def spy(fn):
        def wrapped(p, *a):
            if src in str(p).lower():
                touched.append((fn.__name__, Path(p).name))
            return fn(p, *a)
        return wrapped
    monkeypatch.setattr(shutil, "copyfile", copy)
    monkeypatch.setattr(ingest, "sha256_file", spy(real_sha))
    monkeypatch.setattr(risk, "read_head", spy(real_head))
    monkeypatch.setattr(ingest, "_archive_type", spy(real_type))
    r = ingest.receive(proj, top, "2026-01-01")
    assert r.files == 3 and not r.issues and r.nested_archives == []
    assert sorted(copied) == ["建施-01.pdf", "结构.zip", "说明.txt"] and touched == []
    assert "批次#1 / 网络盘（文件夹） / 结构.zip / 结施-01.pdf" in _paths(proj, 1)


def test_receive_again_after_an_interruption_does_not_copy_again(proj, tmp_path, monkeypatch):
    """收到一半断了（关了窗口、断网）：再收一次，复制进项目的文件不再从来源读，上次留下的文件清单换成这次的。"""
    top = _tree(tmp_path / "图纸", 2, 3)
    real_handle = ingest._Receiver.handle
    calls = []

    def handle(self, *a, **kw):
        calls.append(1)
        if len(calls) == 4:
            raise KeyboardInterrupt
        return real_handle(self, *a, **kw)
    monkeypatch.setattr(ingest._Receiver, "handle", handle)
    with pytest.raises(KeyboardInterrupt):
        ingest.receive(proj, top, "2026-01-01")
    assert list((proj.root / "archives").glob("001-*")) and not proj.connect().execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    monkeypatch.setattr(ingest._Receiver, "handle", real_handle)
    copied = []
    real_copy = shutil.copyfile

    def copy(a, b, **kw):
        copied.append(a)
        return real_copy(a, b, **kw)
    monkeypatch.setattr(shutil, "copyfile", copy)
    r = ingest.receive(proj, top, "2026-01-01")
    assert r.seq == 1 and r.files == 6 and copied == []
    (top / "d0" / "f00.txt").write_bytes(b"changed")  # 改过的文件照样重新复制
    r2 = ingest.receive(proj, top, "2026-01-02")
    assert r2.seq == 2 and [Path(a).name for a in copied] == ["f00.txt"]


def test_folder_label_for_share_and_disk_roots():
    from pathlib import PurePosixPath, PureWindowsPath
    assert ingest.folder_label(PureWindowsPath(r"\\192.168.1.20\图纸")) == "192.168.1.20/图纸"
    assert ingest.folder_label(PureWindowsPath("Z:\\")) == "Z:"
    assert ingest.folder_label(PureWindowsPath(r"\\server\share\建筑")) == "建筑"
    assert ingest.folder_label(PurePosixPath("/srv/图纸")) == "图纸"


# ---------------- 同时收到的几个文件 ----------------

def test_several_files_are_one_batch(proj, tmp_path):
    a, b, c = tmp_path / "建筑.zip", tmp_path / "结构.zip", tmp_path / "补充说明.pdf"
    a.write_bytes(_zip({"建施-01.pdf": _pdf("建施-01", "总说明"), "内层.zip": _zip({"建施-02.pdf": _pdf("建施-02", "平面")})}))
    b.write_bytes(_zip({"结施-01.pdf": _pdf("结施-01", "总说明")}))
    c.write_bytes(_pdf("建施-03", "补充"))
    r = ingest.receive(proj, [a, b, c], "2026-01-01")
    assert r.source == "files" and r.name == "建筑 等 3 个文件" and r.files == 4 and not r.issues
    got = _paths(proj, 1)
    assert "批次#1 / 建筑.zip / 建施-01.pdf" in got and "批次#1 / 补充说明.pdf" in got
    assert r.nested_archives == ["批次#1 / 建筑.zip / 内层.zip"]
    with proj.connect() as con:
        assert con.execute("SELECT archive_name FROM batches").fetchone()[0] == "建筑.zip、结构.zip、补充说明.pdf"
    with pytest.raises(ValueError, match="已经收过了"):
        ingest.receive(proj, [c, b, a], "2026-01-02")  # 顺序不同也认得出是同一批
    with pytest.raises(ValueError, match="一次只能收一个文件夹"):
        ingest.receive(proj, [a, tmp_path], "2026-01-02")


def test_cli_receive_folder_and_files(proj, tmp_path, capsys):
    folder, when = make_sample.build_folder(tmp_path / "in")
    cli.main(["-p", str(proj.root), "receive", str(folder), "--date", when, "--no-av"])
    assert "（文件夹）：收进 4 个文件" in capsys.readouterr().out
    one, two = tmp_path / "一.pdf", tmp_path / "二.pdf"
    one.write_bytes(_pdf("水施-01", "说明"))
    two.write_bytes(_pdf("水施-02", "平面"))
    cli.main(["-p", str(proj.root), "receive", str(one), str(two), "--date", when, "--no-av"])
    assert "（2 个文件）：收进 2 个文件" in capsys.readouterr().out


def test_inner_archive_rule():
    f = state._in_nested_archive
    assert f("批次#3 / 外层.zip / 结构 / 补充图纸.zip / 结施-03.pdf")
    assert not f("批次#3 / 外层.zip / 结构 / 结施-03.pdf")
    assert not f("批次#5 / 补充（文件夹） / 电气.zip / 电施-02.pdf")
    assert f("批次#5 / 补充（文件夹） / 电气.zip / 补充 / 应急.zip / 电施-03.pdf")
    assert not f("批次#6 / 结构.zip / 结施-01.pdf") and not f("批次#6 / 补充说明.pdf")


# ---------------- 被杀毒软件拦下的文件 ----------------

def _blocked_receive(proj, tmp_path, monkeypatch):
    """模拟实时防护：解出来的“病毒.com”一读就报错（Windows 上 Python 会把它报成参数错误）。"""
    real = ingest.sha256_file

    def guarded(p):
        if str(p).endswith("病毒.com"):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real(p)
    monkeypatch.setattr(ingest, "sha256_file", guarded)
    z = tmp_path / "带毒.zip"
    z.write_bytes(_zip({"结施-01.pdf": _pdf("结施-01", "总说明"), "说明/病毒.com": b"placeholder"}))
    lines = []
    ops.receive(proj, z, "2026-01-01", av=False, log=lines.append)
    return lines


def test_blocked_file_is_a_high_risk_file(proj, tmp_path, monkeypatch):
    lines = _blocked_receive(proj, tmp_path, monkeypatch)
    assert any("被本机杀毒软件拦下" in ln for ln in lines)
    assert any(ln.startswith("  ✗ 高风险") and "病毒.com" in ln for ln in lines)
    assert not any("⚠ 被拦截" in ln for ln in lines)
    st = state.build(proj, with_diff=False)
    risky = [i for i in st["issues"] if i["kind"] == "风险文件"]
    assert [i["title"] for i in risky] == ["病毒.com（被杀毒软件拦截）"] and risky[0]["level"] == "crit"
    assert "Invalid argument" not in risky[0]["detail"] and "重新发一份干净的" in risky[0]["detail"]
    assert not any(i["kind"] == "解包问题" for i in st["issues"])
    sec = st["security"]
    assert sec["pending"] == 1 and sec["files"][0]["blocked"] and sec["files"][0]["sources"] == ["本机杀毒软件"]
    assert "病毒.com" in reports.missing_report(st)
    # 决定和普通风险文件一样做
    add_decision(proj, "risk", risky[0]["options"][1]["key"], "dangerous")
    st = state.build(proj, with_diff=False)
    assert st["security"]["pending"] == 0 and "病毒.com" in reports.qa_report(st)
    report = (proj.root / "reports" / "批次01-解包报告.md").read_text(encoding="utf-8")
    assert "病毒.com" in report and "| 被拦截 |" not in report


def test_windows_virus_error_is_named(proj, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_win_open_error", lambda p: 225)  # ERROR_VIRUS_INFECTED
    folder = tmp_path / "图纸"
    folder.mkdir()
    (folder / "坏.dwg").write_bytes(b"AC1032")
    real = ingest.sha256_file

    def guarded(p):
        if str(p).endswith("坏.dwg"):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real(p)
    monkeypatch.setattr(ingest, "sha256_file", guarded)
    r = ingest.receive(proj, folder, "2026-01-01")
    assert r.issues[0]["kind"] == "blocked" and "含有病毒" in r.issues[0]["detail"]


def test_old_projects_blocked_rows_still_show(proj):
    with proj.connect() as con:  # 旧版本登记的“被拦截”解包问题
        con.execute("INSERT INTO batches(seq, name, received_at, archive_name, archive_sha256, stored_path) VALUES (1,'x','2026-01-01','x.zip','0','archives/x.zip')")
        con.execute("INSERT INTO extraction_issues(batch_seq, path, kind, detail) VALUES (1, '批次#1 / x.zip / a.exe', 'blocked', '文件读不出来（Invalid argument），可能已被杀毒软件拦截或隔离')")
    st = state.build(proj, with_diff=False)
    assert [f["name"] for f in st["security"]["files"]] == ["a.exe"] and st["security"]["pending"] == 1
    assert risk.batch_summary(proj, 1)["files"][0]["level"] == "high"


def test_7z_member_that_vanishes_after_extraction(proj, tmp_path, monkeypatch):
    py7zr = pytest.importorskip("py7zr")
    out = tmp_path / "补充.7z"
    with py7zr.SevenZipFile(out, "w") as z:
        z.writestr(_pdf("结施-07", "梁"), "结构/结施-07.pdf")
        z.writestr(b"placeholder", "结构/病毒.com")
    real = py7zr.SevenZipFile.extractall

    def extract_then_quarantine(self, path=None, **kw):
        real(self, path, **kw)
        (Path(path) / "结构" / "病毒.com").unlink()  # 实时防护在解压后把它删了
    monkeypatch.setattr(py7zr.SevenZipFile, "extractall", extract_then_quarantine)
    r = ingest.receive(proj, out, "2026-01-01")
    assert r.files == 1
    assert [(i["kind"], i["path"]) for i in r.issues] == [("blocked", "批次#1 / 补充.7z / 结构 / 病毒.com")]


def test_external_tool_count_check(proj, tmp_path, monkeypatch):
    def fake_extract(archive, dest, kind, passwords):
        (dest / "结施-08.pdf").write_bytes(_pdf("结施-08", "板"))
        return extractors.Outcome(True, "bsdtar")
    monkeypatch.setattr(extractors, "extract", fake_extract)
    monkeypatch.setattr(extractors, "listed_size", lambda a: None)
    monkeypatch.setattr(extractors, "count_files", lambda a, tool, pwd=None: 2)
    rar = tmp_path / "图纸.rar"
    rar.write_bytes(b"Rar!\x1a\x07\x01\x00 fake")
    r = ingest.receive(proj, rar, "2026-01-01")
    assert r.files == 1 and r.issues[0]["kind"] == "blocked" and "少了 1 个" in r.issues[0]["detail"]


# ---------------- 连文件名都加密的 7z ----------------

@pytest.mark.parametrize("header", [True, False])
def test_encrypted_7z(proj, tmp_path, header):
    py7zr = pytest.importorskip("py7zr")
    out = tmp_path / ("加密文件名.7z" if header else "加密内容.7z")
    with py7zr.SevenZipFile(out, "w", password="正确密码", header_encryption=header) as z:
        z.writestr(_pdf("结施-09", "楼板"), "结施-09.pdf")
    r = ingest.receive(proj, out, "2026-01-01")
    assert r.files == 0 and r.issues[0]["kind"] == "encrypted" and "需要密码" in r.issues[0]["detail"]
    assert ("连文件名都加密了" in r.issues[0]["detail"]) == header
    r = ingest.receive(Project.init(tmp_path / "p2", "t"), out, "2026-01-01", passwords=["不对"])
    assert r.files == 0 and "密码都不对" in r.issues[0]["detail"]
    r = ingest.receive(Project.init(tmp_path / "p3", "t"), out, "2026-01-01", passwords=["不对", "正确密码"])
    assert r.files == 1 and not r.issues


# ---------------- 找解压工具 ----------------

@pytest.fixture
def fresh_tools():
    """查找结果有缓存：前后都清掉，换掉的查找函数（没有缓存）跳过。"""
    def clear():
        for name in ("find_7z", "find_unrar", "find_bsdtar"):
            getattr(getattr(extractors, name), "cache_clear", lambda: None)()
    clear()
    yield
    clear()


def test_tools_installed_elsewhere_are_found_via_registry(fresh_tools, monkeypatch):
    seen = {}
    monkeypatch.setattr(extractors, "_registry_values", lambda key, names: {
        r"SOFTWARE\7-Zip": [r"C:\7zip-temp" + "\\"], r"SOFTWARE\WinRAR": [r"D:\Tools\WinRAR\WinRAR.exe"]}[key])
    monkeypatch.setattr(extractors, "_first", lambda *c: seen.setdefault(len(seen), c) and None)
    extractors.find_7z()
    extractors.find_unrar()
    cands = [c for group in seen.values() for c in group]
    assert os.path.join(r"C:\7zip-temp" + "\\", "7z.exe") in cands
    assert os.path.join(os.path.dirname(r"D:\Tools\WinRAR\WinRAR.exe"), "UnRAR.exe") in cands


def test_unreadable_names_ask_for_7zip(fresh_tools, proj, monkeypatch):
    monkeypatch.setattr(extractors, "find_bsdtar", lambda: "bsdtar")
    monkeypatch.setattr(extractors, "find_7z", lambda: None)
    monkeypatch.setattr(extractors, "find_unrar", lambda: None)
    err = "tar.exe: Archive entry has empty or unreadable filename ... skipping.\ntar.exe: Error exit delayed from previous errors"
    monkeypatch.setattr(extractors, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 1, "", err))
    rar = proj.root.parent / "特殊字符.rar"
    rar.write_bytes(b"Rar!\x1a\x07\x01\x00 fake")
    r = ingest.receive(proj, rar, "2026-01-01")
    assert r.issues[0]["kind"] == "no_tool" and "安装 7-Zip" in r.issues[0]["detail"]


def test_count_files_parses_tool_listings(monkeypatch):
    tar = "-rw-r--r--  0 0 0 6 Jul 21  2020 a.txt\ndrwxr-xr-x  0 0 0 0 Jul 21  2020 sub/\nlrwxrwxrwx  0 0 0 0 Jul 21  2020 l -> a.txt\n"
    sz = ("Path = dir\\a.txt\nAttributes = A -rw-------\n\nPath = dir\nAttributes = D drwxr-xr-x\n\n"
          "Path = b.txt\nAttributes = A\n\nPath = link\nAttributes = A lrwxrwxrwx\n")
    monkeypatch.setattr(extractors, "find_bsdtar", lambda: "bsdtar")
    monkeypatch.setattr(extractors, "find_7z", lambda: "7z")
    monkeypatch.setattr(extractors, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, tar if cmd[0] == "bsdtar" else sz, ""))
    assert extractors.count_files(Path("x.rar"), "bsdtar") == 1
    assert extractors.count_files(Path("x.rar"), "7-Zip", "pw") == 2
    assert extractors.count_files(Path("x.rar"), "unrar") is None


def test_folder_pdfs_are_read_several_at_once(tmp_path, monkeypatch):
    """收文件夹时几个进程同时读 PDF（几千个 PDF 一个个渲染算指纹要好几个小时）：登记的页面、问题和一个个读的一样，
    加密的、坏的照样登记；压缩包里的 PDF 照原来那样登记时读。"""
    import pymupdf
    root, date = make_sample.build_folder(tmp_path / "in")
    (root / "建筑" / "坏的.pdf").write_bytes(b"\0garbage" * 50)
    doc = pymupdf.open(stream=make_sample.sheet_pdf("建施-20", "加密的图", "A", "2026-10-20", ["x"]), filetype="pdf")
    doc.save(str(root / "建筑" / "建施-20 加密的图.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="2", user_pw="1")
    doc.close()

    def receive(name):
        proj = Project.init(tmp_path / name, name)
        logs = []
        ops.receive(proj, root, date, av=False, log=logs.append)
        with proj.connect() as con:
            pages = [tuple(r) for r in con.execute("SELECT * FROM pages ORDER BY page_id")]
            issues = sorted((a, k, re.sub(r"'[^']*'", "'…'", d)) for a, k, d in con.execute(  # 原因里的文件路径各项目不同
                "SELECT path, kind, detail FROM extraction_issues"))
        return pages, issues, logs

    one = receive("one")
    pools = []
    real = ingest.ProcessPoolExecutor

    def pool(workers, **kw):
        pools.append(workers)
        return real(workers, **kw)

    monkeypatch.setattr(ingest, "PDF_WORKERS", 3)
    monkeypatch.setattr(ingest, "ProcessPoolExecutor", pool)
    three = receive("three")
    assert three[:2] == one[:2] and pools == [3] and any("3 个同时读" in x for x in three[2])
    assert {k for _p, k, _d in one[1]} >= {"encrypted", "corrupt"} and len(one[0]) >= 4
