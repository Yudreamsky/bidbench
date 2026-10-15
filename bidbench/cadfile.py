"""给翻模工具一份 DXF：一张图现行版的 DWG 用 LibreDWG 的 dwg2dxf 转成 DXF（docs/MCP.md“接 Revit”）。

Revit MCP 的 read_cad / cad_to_model / compare_cad_with_model 只认 DXF。转出来的放在项目的 exports/dxf 里，
同一个文件转过一次就直接用；转失败时把没写完的文件删掉。DXF 原文件复制一份过去，名字好认。
外部参照不会合进 DXF（dwg2dxf 不绑定），同项目里收到了同名的 DWG 就一并转好，列出来让翻模的 agent 自己叠。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import debuglog, dwg, proc
from .store import Project

log = logging.getLogger(__name__)


class CadError(Exception):
    """转不了：给人看的原因。"""


def find_tool() -> str | None:
    """dwg2dxf 在哪：环境变量 BIDBENCH_DWG2DXF > 安装包里附带的 > 系统里装的。"""
    env = os.environ.get("BIDBENCH_DWG2DXF")
    if env:
        return env if Path(env).exists() else None
    exe = "dwg2dxf.exe" if os.name == "nt" else "dwg2dxf"
    for b in (Path(getattr(sys, "_MEIPASS", "") or "."), Path(sys.executable).parent):
        if (b / "libredwg" / exe).exists():
            return str(b / "libredwg" / exe)
    return shutil.which("dwg2dxf")


def out_dir(proj: Project) -> Path:
    return proj.root / "exports" / "dxf"


def to_dxf(proj: Project, src: Path, sha: str, name: str) -> Path:
    """转成 exports/dxf/原文件名-哈希前 8 位.dxf（转过的直接用）；本来是 DXF 的复制一份，名字好认。"""
    ext = src.suffix.lower()
    if ext not in (".dwg", ".dxf"):
        raise CadError(f"{name} 不是 DWG 或 DXF")
    out = out_dir(proj) / f"{Path(name.replace(chr(92), '/')).stem.strip()}-{sha[:8]}.dxf"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".dxf":
        tmp = out.with_name(out.stem + ".part.dxf")
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        return out
    tool = find_tool()
    if not tool:
        raise CadError("没有找到 LibreDWG 的 dwg2dxf，转不了 DXF；请在 AutoCAD 里把这个 DWG 另存为 DXF")
    tmp = out.with_name(out.stem + ".part.dxf")
    cmd = [tool, "-y", "-o", str(tmp), str(src)]
    log.debug("DWG 转 DXF：%s", debuglog.safe_command(cmd))
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=dwg.timeout_for(src.stat().st_size),
                           **proc.NO_WINDOW)
        if not tmp.exists() or tmp.stat().st_size == 0:
            log.debug("dwg2dxf 退出码 %s，没有输出", p.returncode)
            raise CadError("LibreDWG 转不出这个 DWG（可能损坏，或版本太新）；请在 AutoCAD 里另存为 DXF")
        os.replace(tmp, out)  # dwg2dxf 遇到不认识的对象会报错退出，但已写出的 DXF 照样能用
    except subprocess.TimeoutExpired:
        raise CadError("转了太久还没转完，跳过；请在 AutoCAD 里另存为 DXF") from None
    except OSError as e:
        raise CadError(f"LibreDWG 启动不了：{e}") from None
    finally:
        tmp.unlink(missing_ok=True)
    return out


def _source(proj: Project, page_id: str):
    with proj.connect() as con:
        return con.execute("SELECT f.sha256, f.stored_path, f.ext, MIN(o.path) AS name FROM pages p"
                           " JOIN files f ON f.sha256=p.file_sha256 JOIN occurrences o ON o.sha256=f.sha256"
                           " WHERE p.page_id=? GROUP BY f.sha256", (page_id,)).fetchone()


def _same_file(st: dict, sha: str, no: str) -> list[str]:
    """同一个 CAD 文件里的其他图（一个 DWG 常有好几张图）。"""
    out = []
    for s in st["sheets"]:
        for v in s["versions"]:
            if s["no"] != no and any(p and p.startswith(sha[:12]) for p in v.get("page_ids") or [v["page_id"]]):
                out.append(s["no"])
    return sorted(set(out))


def _xref_files(proj: Project, names: list[str]) -> dict[str, tuple[str, Path, str] | None]:
    """外部参照名 → 项目里收到的同名 DWG（sha, 存放路径, 原路径），没收到为 None。"""
    found: dict[str, tuple[str, Path, str] | None] = {n: None for n in names}
    if not names:
        return found
    with proj.connect() as con:
        rows = con.execute("SELECT f.sha256, f.stored_path, o.path FROM files f JOIN occurrences o ON o.sha256=f.sha256"
                           " WHERE f.ext IN ('.dwg', '.dxf') ORDER BY o.batch_seq DESC").fetchall()
    for n in names:
        key = Path(n.replace("\\", "/")).stem.lower()
        for r in rows:
            if Path(r["path"].replace("\\", "/")).stem.lower() == key:
                found[n] = (r["sha256"], proj.root / r["stored_path"], r["path"])
                break
    return found


def for_sheet(proj: Project, st: dict, s: dict) -> dict:
    """一张图的 CAD 文件转成 DXF：优先用现行版本身的 DWG，其次同版次的 DWG，再次旧版的（会提醒）。"""
    cands = []
    for v in s["versions"]:
        for pid in v.get("page_ids") or [v["page_id"]]:
            r = _source(proj, pid) if pid else None
            if r and r["ext"] in (".dwg", ".dxf"):
                rank = 0 if v["state"] == "现行" else 1 if v["rev"] == s["current_rev"] else 2
                cands.append((rank, v, pid, r))
    if not cands:
        raise CadError(f"{s['no']} 没有收到 DWG 或 DXF（只有 PDF）。可以向发图方要 CAD 文件，或列进答疑。")
    rank, v, pid, r = min(cands, key=lambda c: c[0])
    src = proj.root / r["stored_path"]
    dxf = to_dxf(proj, src, r["sha256"], r["name"])
    with proj.connect() as con:
        rec = con.execute("SELECT data FROM recognitions WHERE page_id=?", (pid,)).fetchone()
    content = json.loads(rec["data"])["content"] if rec else {}
    m = re.search(r"（(模型空间|布局“[^”]*”)", content.get("summary") or "")
    out = {"sheet": s["no"], "title": s["title"], "rev": v["rev"], "current_rev": s["current_rev"],
           "page_id": pid, "cad_file": r["name"], "dxf": str(dxf), "where": m.group(1) if m else None,
           "same_file_sheets": _same_file(st, r["sha256"], s["no"])}
    if rank == 2:
        out["warning"] = (f"CAD 文件是 {v['rev'] or '没写版次'} 版，现行是 {s['current_rev'] or '没写版次'} 版，"
                          "翻模后要按现行版 PDF 核对改动")
    xrefs = []
    for name, hit in _xref_files(proj, list(content.get("xrefs") or ())).items():
        if not hit:
            xrefs.append({"name": name, "dxf": None, "note": "没收到这个外部参照文件，轴网、底图可能在里面，列进答疑"})
            continue
        try:
            xrefs.append({"name": name, "dxf": str(to_dxf(proj, hit[1], hit[0], hit[2]))})
        except CadError as e:
            xrefs.append({"name": name, "dxf": None, "note": str(e)})
    if xrefs:
        out["xrefs"] = xrefs
    return out
