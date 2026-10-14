"""打包后的自检：用打出来的 bidbench.exe 跑一遍演示流水线，再连一次 MCP，再读一批 DWG。

python packaging/smoke.py <bidbench.exe 路径> <工作目录>
"""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(exe, *args):
    r = subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"$ bidbench {' '.join(args)}\n{r.stdout}{r.stderr}")
    if r.returncode != 0:
        sys.exit(f"失败：bidbench {' '.join(args)}（退出码 {r.returncode}）")
    return r.stdout


def main() -> None:
    exe, work = Path(sys.argv[1]), Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "samples" / "make_sample.py"), str(work / "zips")], check=True)
    proj = work / "proj"
    run(exe, "--version")
    run(exe, "init", str(proj), "--name", "打包自检")
    dates = ["2026-07-02", "2026-08-02", "2026-09-15", "2026-10-08"]
    names = ["示例综合体投标图纸.zip", "示例综合体投标图纸0802.zip", "0915修改.zip", "1008补充.zip"]
    for name, d in zip(names, dates):
        out = run(exe, "-p", str(proj), "receive", str(work / "zips" / name), "--date", d, "--no-av")
        run(exe, "-p", str(proj), "recognize")
    assert "acad.lsp" in out or (proj / "reports" / "批次03-解包报告.md").read_text(encoding="utf-8").count("acad.lsp")
    out = run(exe, "-p", str(proj), "build")
    assert "19 个图号" in out, out
    run(exe, "-p", str(proj), "export", "current")
    run(exe, "-p", str(proj), "recognize", "--executor", "claude", "--dry-run")  # 只估算：确认 anthropic 和渲染都打包进来了
    asyncio.run(mcp_check(exe, proj))
    dwg_check(exe, work)
    print("自检通过")


def dwg_check(exe, work):
    """安装包里带的 LibreDWG 要能读出 DWG 的图签：收两份合成的 DWG（tests/data/dwg），一份模型空间里两张图，一份三个布局。"""
    folder = work / "dwg-in"
    folder.mkdir()
    for name in ("model.dwg", "layouts.dwg"):
        shutil.copy(ROOT / "tests" / "data" / "dwg" / name, folder / name)
    proj = work / "dwg-proj"
    run(exe, "init", str(proj), "--name", "DWG 自检")
    out = run(exe, "-p", str(proj), "receive", str(folder), "--date", "2026-10-01", "--no-av")
    assert "认出 5 张图" in out, out


async def mcp_check(exe, proj):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=str(exe), args=["-p", str(proj), "mcp"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name for t in (await s.list_tools()).tools}
        assert "project_overview" in tools and "confirm" not in tools, tools
        res = await s.call_tool("project_overview", {})
        data = json.loads(res.content[0].text)
        assert data["as_of_batch"] == 4, data
        print(f"MCP 正常：{len(tools)} 个工具")


if __name__ == "__main__":
    main()
