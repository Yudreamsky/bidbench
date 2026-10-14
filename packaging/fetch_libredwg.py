"""打包前取 LibreDWG 的 Windows 版（读 DWG 用，docs/图纸字段.md 9.6），放进安装包。

只取 dwgread.exe 和它要的 DLL，校验官方发布包的 SHA-256；同时放上许可证（GPLv3）和说明。

python packaging/fetch_libredwg.py <输出目录> [--zip <已下载的发布包>]
"""
import argparse
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.13.3"
URL = f"https://github.com/LibreDWG/libredwg/releases/download/{VERSION}/libredwg-{VERSION}-win64.zip"
SHA256 = "b5133f8b6bd71b7e682a06ef5f99c93b40d628a791c30a31924a80d258c87173"
KEEP = ["dwgread.exe", "libredwg-0.dll", "libiconv-2.dll"]  # dwgread.exe 只依赖 libredwg-0.dll；iconv 转老版本 DWG 的代码页
HERE = Path(__file__).resolve().parent


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--zip")
    a = ap.parse_args(argv)
    out = Path(a.out)
    src = Path(a.zip) if a.zip else out.parent / f"libredwg-{VERSION}-win64.zip"
    if not a.zip:
        src.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=120) as r, open(src, "wb") as f:
            shutil.copyfileobj(r, f)
    got = hashlib.sha256(src.read_bytes()).hexdigest()
    if got != SHA256:
        sys.exit(f"LibreDWG 发布包的 SHA-256 不对：{got}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with zipfile.ZipFile(src) as z:
        for name in KEEP:
            (out / name).write_bytes(z.read(name))
    for name in ("COPYING", "说明.txt"):
        shutil.copy(HERE / "libredwg" / name, out / name)
    print(f"LibreDWG {VERSION} → {out}：{', '.join(sorted(p.name for p in out.iterdir()))}")


if __name__ == "__main__":
    main()
