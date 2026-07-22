"""Kimi CLI provider registration through the generic #103 spawn path."""

from __future__ import annotations

from agent_takkub.provider_install import installable_providers
from agent_takkub.provider_spec import PROVIDER_REGISTRY, kimi_spec


class TestKimiSpec:
    def test_registered(self) -> None:
        assert PROVIDER_REGISTRY["kimi"] is kimi_spec

    def test_binary_names_cover_cross_platform_shims(self) -> None:
        assert kimi_spec.binary_names == ["kimi", "kimi.cmd", "kimi.exe"]

    def test_uv_install_command(self) -> None:
        # Interpreter pinned: kimi-cli supports Python 3.12-3.14 and uv would
        # otherwise resolve whatever default it finds.
        assert kimi_spec.install_command == [
            "uv",
            "tool",
            "install",
            "--python",
            "3.13",
            "kimi-cli",
        ]

    def test_installable(self) -> None:
        assert "kimi" in installable_providers()

    def test_confirmed_autonomy_flag_does_not_mix_modes(self) -> None:
        assert kimi_spec.autonomy_flags == {"default": ["--yolo"]}
        assert "--auto" not in kimi_spec.autonomy_flags["default"]

    def test_tui_markers_remain_an_explicit_gap(self) -> None:
        # Still uncalibrated — no authenticated Kimi TUI captured yet.
        assert kimi_spec.ready_rules == ()

    def test_plants_agents_md_so_teammates_learn_takkub_done(self) -> None:
        # AGENTS.md discovery is confirmed upstream (kimi-cli changelog 1.29.0),
        # so the teammate cheatsheet MUST be planted — without it a kimi pane
        # never learns it has to call `takkub done` and just hangs when finished.
        assert kimi_spec.context_strategy == "agents_md_file"
        assert kimi_spec.cheatsheet_filename == "AGENTS.md"

    def test_windows_shell_requirement_and_login_are_documented(self) -> None:
        assert "Git Bash" in kimi_spec.install_instructions
        # changelog 1.42.0 — the override is KIMI_CLI_GIT_BASH_PATH, not the
        # KIMI_SHELL_PATH this originally shipped with.
        assert "KIMI_CLI_GIT_BASH_PATH" in kimi_spec.install_instructions
        assert "KIMI_SHELL_PATH" not in kimi_spec.install_instructions
        assert "/login" in kimi_spec.post_install_note

    def test_forced_role(self) -> None:
        from agent_takkub import provider_config

        assert provider_config.KIMI == "kimi"
        assert provider_config.provider_for("kimi") == "kimi"
        assert "kimi" in provider_config.FORCED_ROLES
