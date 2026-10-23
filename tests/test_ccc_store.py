from __future__ import annotations

import json
import sys

from agent_takkub import ccc_store


def test_runner_config_is_provider_neutral() -> None:
    config = ccc_store.runner_mcp_config()
    assert config["command"] == sys.executable
    assert config["args"] == ["-m", "agent_takkub.cli", "ccc", "serve"]


def test_ensure_cursor_mcp_preserves_other_servers(tmp_path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "node"}}}), encoding="utf-8"
    )

    ok, message = ccc_store.ensure_cursor_mcp(config_path)

    assert ok and "added" in message
    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["other"] == {"command": "node"}
    assert servers["ccc"] == ccc_store.runner_mcp_config()


def test_ensure_cursor_mcp_refuses_conflicting_name(tmp_path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"ccc": {"command": "other"}}}), encoding="utf-8"
    )

    ok, message = ccc_store.ensure_cursor_mcp(config_path)

    assert not ok
    assert "left unchanged" in message
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["ccc"] == {
        "command": "other"
    }


def test_env_caps_embedding_threads(tmp_path, monkeypatch) -> None:
    """#901/#904: torch's default thread pool took 7-10 cores per refresh."""
    for name in ccc_store._THREAD_CAP_VARS:
        monkeypatch.delenv(name, raising=False)
    env = ccc_store.env_for_root(tmp_path)
    for name in ccc_store._THREAD_CAP_VARS:
        assert env[name] == ccc_store.EMBED_THREAD_CAP
    assert env["TOKENIZERS_PARALLELISM"] == "false"
    monkeypatch.setenv("OMP_NUM_THREADS", "6")
    assert ccc_store.env_for_root(tmp_path)["OMP_NUM_THREADS"] == "6"


def test_pane_config_pins_root_through_runner(tmp_path) -> None:
    config = ccc_store.pane_mcp_config(tmp_path)
    assert config["command"] == ccc_store.runner_mcp_config()["command"]
    assert config["args"] == [*ccc_store.CCC_RUNNER_ARGS, "--cwd", str(tmp_path)]
    assert config["env"]["COCOINDEX_CODE_ROOT_PATH"] == str(tmp_path)


def test_runner_avoids_pythonw(tmp_path, monkeypatch) -> None:
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(pythonw))
    assert ccc_store._console_python() == str(pythonw)
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    assert ccc_store._console_python() == str(python)


def test_serve_starts_launcher_at_low_priority(tmp_path, monkeypatch) -> None:
    """#904: the shared daemon inherits the priority of the `serve` child."""
    import argparse
    import subprocess

    from agent_takkub import _win_console, cli

    monkeypatch.setattr(ccc_store, "ccc_server_path", lambda: "cocoindex-code")
    monkeypatch.setattr(ccc_store, "index_ready", lambda root: True)
    monkeypatch.setattr(_win_console, "gate_popen_kwargs", lambda: {"creationflags": 12345})
    seen: dict = {}

    def fake_call(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    args = argparse.Namespace(ccc_cmd="serve", cwd=str(tmp_path), project=None)
    assert cli.cmd_ccc(args)["ok"]
    assert seen["argv"] == ["cocoindex-code", "serve"]
    assert seen["creationflags"] == 12345
    assert seen["env"]["OMP_NUM_THREADS"]
