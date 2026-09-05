"""#492: a pane's own `takkub` CLI subcommands (`takkub done`, `takkub send`,
...) run as fresh Python subprocesses inside the pane's shell — distinct
from the cockpit's own already-open stdout/stderr that
`cli._ensure_utf8_stdio()` reconfigures. On a Thai-locale Windows box those
fresh subprocesses default to the cp874 console codepage and render Thai
text as mojibake unless PYTHONIOENCODING/PYTHONUTF8 are set in their env.
See `pane_env._apply_utf8_io_env`.
"""

from __future__ import annotations

import agent_takkub.pane_env as pane_env


class TestApplyUtf8IoEnv:
    def test_sets_both_vars_on_empty_env(self) -> None:
        env: dict[str, str] = {}
        pane_env._apply_utf8_io_env(env)
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"

    def test_operator_override_survives(self) -> None:
        env = {"PYTHONIOENCODING": "cp874", "PYTHONUTF8": "0"}
        pane_env._apply_utf8_io_env(env)
        assert env["PYTHONIOENCODING"] == "cp874"
        assert env["PYTHONUTF8"] == "0"


class TestBuildPaneEnvWiring:
    def test_build_pane_env_includes_utf8_io_defaults(self, monkeypatch) -> None:
        from agent_takkub import config

        monkeypatch.setattr(config, "_effective_port_file_for_app", lambda: "port-file")
        env = pane_env._build_pane_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"

    def test_build_pane_env_honours_cockpit_level_override(self, monkeypatch) -> None:
        from agent_takkub import config

        monkeypatch.setattr(config, "_effective_port_file_for_app", lambda: "port-file")
        monkeypatch.setenv("PYTHONIOENCODING", "cp874")
        env = pane_env._build_pane_env()
        assert env["PYTHONIOENCODING"] == "cp874"

    def test_build_lead_env_includes_utf8_io_defaults(self, monkeypatch) -> None:
        from agent_takkub import config

        monkeypatch.setattr(config, "_effective_port_file_for_app", lambda: "port-file")
        env = pane_env._build_lead_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"
