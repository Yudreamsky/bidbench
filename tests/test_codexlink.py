"""把项目接到本机 Codex：写 ~/.codex/config.toml 的 bidbench 一节，其余设置不动。"""

import threading

import pytest

from bidbench import cli, codexlink, server

tomllib = pytest.importorskip("tomllib")


def test_connect_writes_and_replaces(demo_copy, tmp_path):
    cfg = tmp_path / "home" / "config.toml"
    r = codexlink.connect(demo_copy, cfg)  # 文件还没有：新建，不备份
    assert not r["replaced"] and r["backup"] is None
    srv = tomllib.loads(cfg.read_text(encoding="utf-8"))["mcp_servers"]["bidbench"]
    assert srv["args"][-3:] == ["-p", str(demo_copy.root), "mcp"]
    assert srv["startup_timeout_sec"] == 60 and srv["env"]["PYTHONUTF8"] == "1"
    assert codexlink.connected_project(cfg) == str(demo_copy.root)

    # 用户自己的设置、别的 MCP 服务、手写的旧 bidbench 一节（带子表）
    cfg.write_text('model = "gpt-6.1-sol"\n\n[mcp_servers.bidbench]\ncommand = "old"\n\n'
                   '[mcp_servers.bidbench.env]\nA = "1"\n\n[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8")
    assert codexlink.connected_project(cfg) == "（手动配置）"
    r = codexlink.connect(demo_copy, cfg)
    assert r["replaced"] and "command = \"old\"" in (tmp_path / "home" / "config.toml.bak").read_text(encoding="utf-8")
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-6.1-sol" and data["mcp_servers"]["other"] == {"command": "x"}
    assert data["mcp_servers"]["bidbench"]["command"] != "old" and "A" not in data["mcp_servers"]["bidbench"]["env"]
    codexlink.connect(demo_copy, cfg)  # 再写一次不会叠出两节
    assert cfg.read_text(encoding="utf-8").count("[mcp_servers.bidbench]") == 1


def test_connect_refuses_broken_config(demo_copy, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("model = \n", encoding="utf-8")
    with pytest.raises(ValueError, match="格式错误"):
        codexlink.connect(demo_copy, cfg)
    assert cfg.read_text(encoding="utf-8") == "model = \n"


def test_windows_paths_are_escaped(demo_copy, monkeypatch):
    from bidbench import chat
    monkeypatch.setattr(chat, "mcp_command", lambda p: [r"C:\Users\林\AppData\Local\Programs\bidbench\bidbench.exe",
                                                       "-p", r"D:\投标\项目", "mcp"])
    srv = tomllib.loads(codexlink.server_block(demo_copy))["mcp_servers"]["bidbench"]
    assert srv["command"].endswith(r"bidbench\bidbench.exe") and srv["args"][1] == r"D:\投标\项目"


def test_cli_connect_codex(demo_copy, capsys):
    cli.main(["-p", str(demo_copy.root), "connect-codex", "--print"])
    assert "[mcp_servers.bidbench]" in capsys.readouterr().out and not codexlink.config_path().exists()
    cli.main(["-p", str(demo_copy.root), "connect-codex"])
    assert "已把项目接到 Codex" in capsys.readouterr().out
    assert codexlink.connected_project() == str(demo_copy.root)


def test_workbench_connect(demo_copy, monkeypatch):
    from test_desktop import Client
    from bidbench import ops
    monkeypatch.setattr(ops, "codex_status", lambda refresh=False: {"found": False, "ready": False, "hint": "x", "models": []})
    httpd = server.serve(demo_copy, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd)
    try:
        assert c.get("/api/chat?status=1")["codex_link"]["project"] is None
        assert c.post("/api/codex/connect", {}, token=False)["_status"] == 403
        assert c.post("/api/codex/connect", {})["project"] == str(demo_copy.root)
        link = c.get("/api/chat?status=1")["codex_link"]
        assert link["project"] == link["here"]
    finally:
        httpd.shutdown()
