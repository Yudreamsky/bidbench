"""桌面工作台：独立窗口，不用浏览器。

窗口用系统自带的网页引擎（Windows 上是 Edge WebView2）显示和浏览器里一样的工作台页面，
后台在本机随机端口起一个只给这个窗口用的服务。没有 WebView2 时退回用默认浏览器打开。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, appconfig, debuglog, release
from .server import serve
from .store import Project

log = logging.getLogger(__name__)

RECEIVE_TYPES = ("压缩包和图纸 (*.zip;*.rar;*.7z;*.tar;*.tgz;*.gz;*.pdf;*.dwg;*.dxf)", "所有文件 (*.*)")


class DesktopApi:
    """页面里通过 window.pywebview.api 调用的本机功能：系统的选文件、选文件夹对话框。"""

    def __init__(self):
        self._window = None  # 下划线开头：pywebview 不会把它暴露给页面

    def pick_files(self):
        """收图用：可以一次选多个文件（同一次收到的放在一批）。"""
        import webview
        r = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=RECEIVE_TYPES)
        return list(r) if r else None

    def pick_folder(self, start: str = ""):
        import webview
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start or "")
        return r[0] if r else None


def _redirect_output() -> None:
    """打包成无控制台的窗口程序时没有 stdout / stderr，写到排错日志文件夹里，免得第三方库输出时出错。"""
    if sys.stdout is None or sys.stderr is None:
        path = debuglog.folder() / "desktop-output.log"
        try:
            if path.stat().st_size > 5 << 20:  # 太大时从头写，旧的留一份
                os.replace(path, path.with_suffix(".old.log"))
        except OSError:
            pass
        out = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or out
        sys.stderr = sys.stderr or out


def _initial_project(arg: str | None) -> Project | None:
    cand = arg or appconfig.load().get("last_project")
    if cand and (Path(cand) / "bidbench.json").exists():
        try:
            proj = Project.open(Path(cand))
            proj.connect().close()
            return proj
        except Exception:  # noqa: BLE001 - 打不开就回到项目选择界面
            return None
    return None


def main(argv=None) -> int:
    _redirect_output()
    ap = argparse.ArgumentParser(prog="bidbench-desktop", description="投标图纸工作台（桌面窗口）")
    ap.add_argument("project", nargs="?", help="要打开的项目目录（默认打开上次的项目）")
    ap.add_argument("--browser", action="store_true", help="不开独立窗口，用默认浏览器打开")
    ap.add_argument("--smoke", action="store_true", help="自检：启动服务、检查接口后退出（打包测试用）")
    args = ap.parse_args(argv)
    debuglog.setup("desktop", hooks=not args.smoke)

    appconfig.apply_secrets()
    httpd = serve(_initial_project(args.project), port=0, mode="desktop")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if args.smoke:
        info = json.loads(urllib.request.urlopen(url + "api/app", timeout=10).read())  # noqa: S310 - 本机
        page = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")  # noqa: S310
        ok = info["mode"] == "desktop" and httpd.app.token in page
        print(json.dumps({"ok": ok, "version": __version__, "build": release.current_build(),
                          "installed": info["installed"]}, ensure_ascii=False))
        httpd.shutdown()
        return 0 if ok else 1

    if not args.browser:
        try:
            return _window(httpd, url)
        except Exception as e:  # noqa: BLE001 - 没有 WebView2 等，退回浏览器
            log.warning("无法打开独立窗口，改用浏览器", exc_info=True)
            print(f"无法打开独立窗口（{e}），改用浏览器。", file=sys.stderr)
    webbrowser.open(url)
    print(f"工作台已在浏览器中打开：{url}（关闭这个窗口即退出）")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    httpd.app.close_down()
    httpd.shutdown()
    return 0


def _window(httpd, url: str) -> int:
    import webview

    api = DesktopApi()
    window = webview.create_window("投标图纸工作台", url, js_api=api, width=1440, height=920, min_size=(1000, 640),
                                   text_select=True, zoomable=True)
    api._window = window
    app = httpd.app
    quitting = []

    def destroy():
        quitting.append(True)
        window.destroy()

    def closing():
        job = app.job
        if quitting or not job or job.status != "running":
            return True
        return window.create_confirmation_dialog("还有操作在进行", f"「{job.title}」还没完成，现在退出会中断它。确定退出吗？")

    window.events.closing += closing
    app.on_quit.append(destroy)
    storage = appconfig.config_dir() / "webview"
    log.info("打开窗口")
    webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=False, storage_path=str(storage))
    log.info("窗口已关闭")
    app.close_down()
    httpd.shutdown()  # 窗口关掉就退出
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
