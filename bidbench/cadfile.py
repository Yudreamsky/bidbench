"""给翻模工具一份 DXF：一张图现行版的 DWG 用 LibreDWG 的 dwg2dxf 转成 DXF（docs/MCP.md“接 Revit”）。

Revit MCP 的 read_cad / cad_to_model / compare_cad_with_model 只认 DXF。转出来的放在项目的 exports/dxf 里，
同一个文件转过一次就直接用；转失败时把没写完的文件删掉。DXF 原文件复制一份过去，名字好认。
外部参照不会合进 DXF（dwg2dxf 不绑定），同项目里收到了同名的 DWG 就一并转好，列出来让翻模的 agent 自己叠。
一个文件里有几张图时，给出这张图在模型空间里的范围（region），Revit MCP 只读范围里的东西（docs/图纸字段.md 9.7 第 7 条）。
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
        if ext == ".dwg":
            _refix(out, src)
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
        fix_anonymous_blocks(tmp, src)
        os.replace(tmp, out)  # dwg2dxf 遇到不认识的对象会报错退出，但已写出的 DXF 照样能用
    except subprocess.TimeoutExpired:
        raise CadError("转了太久还没转完，跳过；请在 AutoCAD 里另存为 DXF") from None
    except OSError as e:
        raise CadError(f"LibreDWG 启动不了：{e}") from None
    finally:
        tmp.unlink(missing_ok=True)
    _mark(out)
    return out


def _entities(lines: list[bytes]):
    """DXF 按实体切开：[(实体在第几行开始, 段名, 类型, [(组码行号, 组码, 值)…])]。"""
    out, section = [], None
    for i in range(0, len(lines) - 1, 2):
        code, val = lines[i].strip(), lines[i + 1].strip()
        if code == b"0":
            if val == b"SECTION":
                section = b"?"
            elif val == b"ENDSEC":
                section = None
            out.append([i, section, val, []])
        elif out:
            if code == b"2" and out[-1][2] == b"SECTION":
                out[-1][1] = section = val
            out[-1][3].append((i, code, val))
    return out


def _block_names(ents) -> tuple[set[bytes], dict[int, bytes]]:
    """DXF 里定义了的块名，和块记录句柄 → 块名（TABLES 的 BLOCK_RECORD；没有的用块定义的 330 组码补）。"""
    def h(v):
        try:
            return int(v, 16)
        except (TypeError, ValueError):
            return None
    defined, by_handle = set(), {}
    for _n, sec, kind, g in ents:
        name = next((v for _i, c, v in g if c == b"2"), b"")
        if sec == b"BLOCKS" and kind == b"BLOCK":
            defined.add(name)
            owner = h(next((v for _i, c, v in g if c == b"330"), None))
            if owner is not None:
                by_handle.setdefault(owner, name)
        elif sec == b"TABLES" and kind == b"BLOCK_RECORD":
            own = h(next((v for _i, c, v in g if c == b"5"), None))
            if own is not None and name:
                by_handle[own] = name
    return defined, by_handle


def broken_blocks(lines: list[bytes]) -> list[tuple[int, int]]:
    """引用了不存在的匿名块的块参照：[(块名那一行, 句柄)]。"""
    ents = _entities(lines)
    defined, _ = _block_names(ents)
    bad = []
    for _n, _sec, kind, g in ents:
        if kind not in (b"INSERT", b"MINSERT"):
            continue
        name = next(((i, v) for i, c, v in g if c == b"2"), None)
        handle = next((v for _i, c, v in g if c == b"5"), None)
        if name and name[1].startswith(b"*") and name[1] not in defined:
            try:
                bad.append((name[0], int(handle, 16) if handle else -1))
            except ValueError:
                bad.append((name[0], -1))
    return bad


BLOCKFIX_VERSION = 2  # 补块名的办法改了就加 1，转好存着的 DXF 再补一遍


def fix_anonymous_blocks(dxf: Path, src: Path) -> int:
    """dwg2dxf 把动态块参照的匿名块名写成“*U”（块定义还在，叫 *U4、*U6），轴号、轴线这类动态块就全丢了。
    dwgread 读出的名字也是“*U”，所以只拿它给的“块参照用哪条块记录”（句柄），名字到 DXF 自己的块记录表里找；
    找到的名字有块定义才算补上。返回补了几个，补不了的留着（cad_file 会提醒）。可以反复跑。"""
    try:
        lines = dxf.read_bytes().split(b"\n")
    except OSError:
        return 0
    bad = broken_blocks(lines)
    if not bad:
        return 0
    try:
        refs = dwg.insert_blocks(src)
    except dwg.ReadError as e:
        log.debug("补匿名块名时 DWG 读不出：%s", e)
        return 0
    defined, by_handle = _block_names(_entities(lines))
    fixed = 0
    for line, handle in bad:
        name = by_handle.get(refs.get(handle, -1))
        if name and name in defined:
            end = b"\r" if lines[line + 1].endswith(b"\r") else b""
            lines[line + 1] = name + end
            fixed += 1
    if fixed:
        tmp = dxf.with_name(dxf.name + ".fix")
        try:
            tmp.write_bytes(b"\n".join(lines))
            os.replace(tmp, dxf)
        finally:
            tmp.unlink(missing_ok=True)
    log.debug("匿名块名：%s 个不对，补回 %s 个", len(bad), fixed)
    return fixed


def _refix(out: Path, src: Path) -> None:
    """以前转好的 DXF 是用旧办法补的块名：再补一遍，记下补过了；补出东西来，算好的范围作废。"""
    try:
        if out.with_name(out.stem + ".blocks").read_text(encoding="utf-8").strip() == str(BLOCKFIX_VERSION):
            return
    except OSError:
        pass
    if fix_anonymous_blocks(out, src):
        out.with_name(out.stem + ".regions.json").unlink(missing_ok=True)
    _mark(out)


def _mark(out: Path) -> None:
    try:
        out.with_name(out.stem + ".blocks").write_text(str(BLOCKFIX_VERSION), encoding="utf-8")
    except OSError as e:
        log.debug("补块名的记号存不下：%s", e)


REGION_VERSION = 3  # 算范围的规则改了就加 1，存着的范围重算


def _box(b) -> list[float]:
    return [round(float(x), 3) for x in b]


def _is_frame(sh: dwg.Sheet) -> bool:
    """图签块的外框是不是整张图的图框：归这张图的字铺开的范围比它大十倍以上的，它只是个图名标签。"""
    pts = [(t.x, t.y) for t in sh.texts] + [sh.box[:2], sh.box[2:]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    spread = (max(xs) - min(xs)) * (max(ys) - min(ys))
    return (sh.box[2] - sh.box[0]) * (sh.box[3] - sh.box[1]) * 10 >= spread


def _region(d: dwg.Drawing, sh: dwg.Sheet) -> dict:
    """一张图在模型空间里的范围：图框（模型空间里排图签的），或布局视口看到的地方。"""
    if sh.space == dwg.MODEL:
        if sh.attrs and sh.box and _is_frame(sh):  # 图签块的外框；没有图签块时 box 只是文字的范围，不能当图框
            return {"region": [_box(sh.box)]}
        if sh.attrs:  # 图上的字大多在“图签块”外面，或块里没有线：它只是个图名标签，不是图框
            return {"note": "这张图的图签块只是个小标签，不是图框，框不出这张图的范围，翻模时请在 CAD 里单独存出这张图"}
        return {"note": "框不出这张图的范围（图签转了角度，或没有图签块），翻模时请在 CAD 里单独存出这张图"}
    vps = [v for v in d.viewports if v.space == sh.space]
    if not vps:
        return {"note": "这张图画在布局上，没有看模型空间的视口；Revit MCP 只读模型空间，请在 CAD 里把图移到模型空间再转"}
    out: dict = {"region": [_box(v.window()) for v in vps]}
    if any(v.twist % 90 for v in vps):
        out["note"] = "视口转了角度，范围取的是外接矩形，可能带进旁边的一点东西"
    return out


def regions(dxf: Path) -> dict:
    """转好的 DXF 里每张图的图号和范围，和引用了不存在的块的块参照有几个：软件自己读 DXF、分图（和读 DWG 同一套分法），
    算一次存在 DXF 旁边。"""
    cache = dxf.with_name(dxf.stem + ".regions.json")
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("version") == REGION_VERSION:
            return data
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    try:
        d = dwg.read_dxf(dxf)
        sheets = dwg.split_sheets(d)
        broken = len(broken_blocks(dxf.read_bytes().split(b"\n")))
    except (dwg.ReadError, OSError) as e:
        log.debug("算翻模范围时 DXF 读不出：%s", e)
        return {"sheets": [], "broken": 0}
    out = []
    for sh in sheets:
        no = sh.fields.get("drawing_no")
        out.append({"no": dwg._norm_no(no) if no else None, "space": sh.space, **_region(d, sh)})
    data = {"version": REGION_VERSION, "sheets": out, "broken": broken}
    tmp = cache.with_name(cache.name + ".part")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError as e:
        log.debug("翻模范围存不下：%s", e)
    finally:
        tmp.unlink(missing_ok=True)
    return data


def _match(found: list[dict], rec: dict) -> dict | None:
    """识别结果对应 DXF 里的哪张图：先按图号，图数一样再按第几张。"""
    no = ((rec.get("title_block") or {}).get("drawing_no") or {}).get("value")
    if no:
        hit = [r for r in found if r["no"] and r["no"].upper() == dwg._norm_no(no).upper()]
        if len(hit) == 1:
            return hit[0]
    src = rec.get("source") or {}
    i = src.get("page_index")
    if isinstance(i, int) and len(found) == src.get("page_count") and 0 <= i < len(found):
        return found[i]
    return None


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
    data = json.loads(rec["data"]) if rec else {}
    content = data.get("content") or {}
    m = re.search(r"（(模型空间|布局“[^”]*”)", content.get("summary") or "")
    out = {"sheet": s["no"], "title": s["title"], "rev": v["rev"], "current_rev": s["current_rev"],
           "page_id": pid, "cad_file": r["name"], "dxf": str(dxf), "where": m.group(1) if m else None,
           "same_file_sheets": _same_file(st, r["sha256"], s["no"])}
    info = regions(dxf)
    found = info["sheets"]
    if info.get("broken"):
        out["dxf_warning"] = (f"转出的 DXF 里有 {info['broken']} 个块参照找不到它用的块（多半是动态块：轴号、轴线、门窗），"
                              "Revit MCP 读不到它们；请在 AutoCAD 里把这个 DWG 另存为 DXF 再翻模")
    hit = _match(found, data) if data else None
    if hit and hit.get("region"):
        out["region"] = hit["region"]
    if len(found) > 1 or out["same_file_sheets"]:
        if hit and hit.get("note"):
            out["region_note"] = hit["note"]
        elif not hit:
            out["region_note"] = "这个文件里有几张图，没对上是哪一块，翻模时请在 CAD 里单独存出这张图"
    elif hit and hit.get("note") and hit["space"] != dwg.MODEL and not hit.get("region"):
        out["region_note"] = hit["note"]  # 只有一张图、但画在布局上：Revit MCP 读不到
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
