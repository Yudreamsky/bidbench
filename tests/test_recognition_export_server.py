import json
import threading
import urllib.request

import jsonschema
import pymupdf

from bidbench import export, recognition, state
from bidbench.server import serve


def test_simulated_results_match_schema(demo):
    schema = recognition.load_schema()
    with demo.connect() as con:
        rows = con.execute("SELECT data FROM recognitions").fetchall()
    assert len(rows) == 27
    for r in rows:
        jsonschema.validate(json.loads(r["data"]), schema)


def test_import_rejects_bad_results(demo_copy, tmp_path):
    d = tmp_path / "results"
    d.mkdir()
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "wrong.json").write_text(json.dumps({"schema_version": "0.1"}), encoding="utf-8")
    res = recognition.import_results(demo_copy, d)
    assert res.imported == 0 and len(res.errors) == 2
    assert (demo_copy.root / "reports" / "识别结果导入问题.md").exists()


def test_task_package(demo_copy):
    with demo_copy.connect() as con:
        con.execute("DELETE FROM recognitions")
    task = recognition.make_task(demo_copy, render=True)
    manifest = json.loads((task / "manifest_in.json").read_text(encoding="utf-8"))
    # 像素完全相同的页面只识别一次
    pids = [p["page_id"] for p in manifest["pages"]]
    assert len(pids) == len(set(pids))
    first = manifest["pages"][0]
    assert (task / first["image"]).exists() and (task / first["titleblock_image"]).exists()
    assert (task / "任务说明.md").exists() and (task / "schema.json").exists()


def test_export_current(demo, tmp_path):
    st = state.build(demo, with_diff=False)
    out = export.export_current(demo, st, tmp_path / "cur")
    files = {p.name for p in out.rglob("*.pdf")}
    assert "建施-07 1-1剖面图 A版.pdf" in files  # 从合订本里单独抽出
    assert not any("建施-09" in f for f in files)  # 缺失的图不会出现
    assert "电施-01 照明平面图 B版.pdf" in files  # 同为 B 版、内容不同：按日期定了现行，只是提醒改图没升版，不标冲突
    doc = pymupdf.open(next(out.rglob("建施-07*.pdf")))
    assert doc.page_count == 1


def test_export_flags_unsettled_conflict(sheets_project, tmp_path):
    """同版次、同日期、内容不同，拿不准现行的是哪份：导出时文件名标【冲突】。"""
    import make_sample
    h = [("A", "2026-01-01", "出图")]
    proj = sheets_project({"建施-02 一层平面图.pdf": make_sample.sheet_pdf("建施-02", "一层平面图", "A", "2026-01-01", [], h),
                           "建施-02 一层平面图（2）.pdf": make_sample.sheet_pdf("建施-02", "一层平面图", "A", "2026-01-01", [], h,
                                                                       variant=1)})
    out = export.export_current(proj, state.build(proj, with_diff=False), tmp_path / "cur")
    assert any(p.name.startswith("【冲突】建施-02") for p in out.rglob("*.pdf"))


def test_export_mirror(demo, tmp_path):
    st = state.build(demo, with_diff=False)
    out = export.export_mirror(demo, st, tmp_path / "m")
    assert (out / "L0-项目地图.md").exists() and (out / "facts.json").exists()
    card = (out / "L1-图纸卡片" / "结施-04.md").read_text(encoding="utf-8")
    assert "C 版" in card and "已替代" in card


def test_server(demo_copy):
    httpd = serve(demo_copy, port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        st = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert st["as_of"] == 4
        old = json.loads(urllib.request.urlopen(base + "/api/state?as_of=1").read())
        assert old["as_of"] == 1
        page = st["sheets"][0]["current_page"]
        pdf = urllib.request.urlopen(base + f"/api/page/{page}.pdf").read()
        assert pdf.startswith(b"%PDF")
        req = urllib.request.Request(base + "/api/decision", method="POST",
                                     data=json.dumps({"kind": "sheet_void", "key": "建施-05", "value": "voided"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Bidbench-Token": httpd.app.token})
        st2 = json.loads(urllib.request.urlopen(req).read())
        assert next(s for s in st2["sheets"] if s["no"] == "建施-05")["status"] == "voided"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "投标图纸工作台" in html
    finally:
        httpd.shutdown()


def test_web_script_parses(tmp_path):
    """工作台页面的内联脚本能通过语法检查（CI 上有 Node）。"""
    import shutil
    import subprocess
    from importlib import resources

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 Node，跳过脚本语法检查")
    html = resources.files("bidbench").joinpath("web/index.html").read_text(encoding="utf-8")
    js = html[html.index("<script>") + len("<script>"): html.rindex("</script>")]
    f = tmp_path / "page.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cli_output_survives_legacy_codepage(demo_copy):
    """Windows 上输出被重定向时默认是本地代码页，中文和 ✗ ⚠ 等符号不能让命令崩溃。"""
    import os
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "bidbench", "-p", str(demo_copy.root), "build"], capture_output=True,
                       env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "图号" in r.stdout.decode("utf-8")
