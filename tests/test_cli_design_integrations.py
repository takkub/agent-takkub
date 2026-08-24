"""`takkub design integrations status|enable|disable|doctor` (#373) —
local (non-IPC) CLI surface, same shape/gate as `test_cli_pane_tools.py`'s
`takkub mcp allow/deny`: enable/disable require lead, status stays
read-only for everyone. No real network call anywhere in this suite —
`enable`/`status`/`doctor` never construct a `design_clients` client."""

from __future__ import annotations

import pytest

from agent_takkub import cli, pane_tools_policy, shared_dev_tools


def _clear_role_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT"):
        monkeypatch.delenv(key, raising=False)


class TestStatusIsOpenToEveryone:
    def test_teammate_can_run_status(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset()
        )

        code = cli.main(["design", "integrations", "status"])

        assert code == 0
        out = capsys.readouterr().out
        assert "figma" in out
        assert "penpot" in out
        assert "reference-21st" in out

    def test_status_filters_to_single_id(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset()
        )

        code = cli.main(["design", "integrations", "status", "figma"])

        assert code == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("figma")

    def test_status_unknown_id_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)

        code = cli.main(["design", "integrations", "status", "not-a-real-one"])

        assert code == 1

    def test_status_with_role_shows_enabled_flag(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset({"figma"})
        )

        code = cli.main(["design", "integrations", "status", "figma", "--role", "qa"])

        assert code == 0
        out = capsys.readouterr().out
        assert "enabled=True" in out


class TestEnableDisableAreLeadOnly:
    @pytest.mark.parametrize(
        "argv",
        [
            ["design", "integrations", "enable", "figma", "--role", "qa"],
            ["design", "integrations", "disable", "figma", "--role", "qa"],
        ],
    )
    def test_teammate_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, argv: list[str]
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        calls: list[str] = []
        monkeypatch.setattr(
            pane_tools_policy, "allow_item", lambda *a: calls.append("allow") or True
        )
        monkeypatch.setattr(pane_tools_policy, "deny_item", lambda *a: calls.append("deny") or True)

        code = cli.main(argv)

        assert code == 1
        assert calls == []
        assert "only lead" in capsys.readouterr().out

    def test_lead_can_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[tuple] = []
        monkeypatch.setattr(
            pane_tools_policy,
            "allow_item",
            lambda role, kind, name: calls.append((role, kind, name)) or True,
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["design", "integrations", "enable", "figma", "--role", "qa"])

        assert code == 0
        assert calls == [("qa", "mcps", "figma")]

    def test_enable_unknown_integration_id_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")

        code = cli.main(["design", "integrations", "enable", "not-a-real-one", "--role", "qa"])

        assert code == 1

    def test_enable_unknown_role_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")

        code = cli.main(["design", "integrations", "enable", "figma", "--role", "nope"])

        assert code == 1

    def test_enable_with_token_stores_via_secret_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(pane_tools_policy, "allow_item", lambda *a: True)
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        from agent_takkub.core.secrets.manager import SecretManager

        seen: dict = {}
        monkeypatch.setattr(
            SecretManager,
            "set_secret",
            lambda self, ref, value: seen.update(ref=ref, value=value),
        )

        code = cli.main(
            ["design", "integrations", "enable", "figma", "--role", "qa", "--token", "sekret"]
        )

        assert code == 0
        assert seen["ref"] == "secret://figma/default"
        assert seen["value"] == "sekret"

    def test_enable_penpot_requires_base_url_with_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(pane_tools_policy, "allow_item", lambda *a: True)
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(
            ["design", "integrations", "enable", "penpot", "--role", "qa", "--token", "tok"]
        )

        assert code == 1

    def test_enable_reference_21st_also_registers_mcp_server(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(pane_tools_policy, "allow_item", lambda *a: True)
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)
        seen: dict = {}
        monkeypatch.setattr(
            shared_dev_tools,
            "add_mcp_server",
            lambda name, cfg, force=False: seen.update(name=name, cfg=cfg, force=force) or True,
        )

        code = cli.main(["design", "integrations", "enable", "reference-21st", "--role", "qa"])

        assert code == 0
        assert seen["name"] == "reference-21st"
        assert seen["force"] is True

    def test_lead_can_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[tuple] = []
        monkeypatch.setattr(
            pane_tools_policy,
            "deny_item",
            lambda role, kind, name: calls.append((role, kind, name)) or True,
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["design", "integrations", "disable", "figma", "--role", "qa"])

        assert code == 0
        assert calls == [("qa", "mcps", "figma")]


class TestDoctorSubcommand:
    def test_doctor_prints_report_and_never_hits_network(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path
    ) -> None:
        import json

        import agent_takkub.config as cfg

        _clear_role_env(monkeypatch)
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        projects_file = tmp_path / "projects.json"
        projects_file.write_text(json.dumps({"projects": {}}), encoding="utf-8")
        monkeypatch.setattr(cfg, "PROJECTS_JSON", projects_file)

        import urllib.request

        def _boom(*a, **kw):  # pragma: no cover — only invoked if a real call is attempted
            raise AssertionError("doctor must never touch the network")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)

        code = cli.main(["design", "integrations", "doctor"])

        assert code == 0
        out = capsys.readouterr().out
        assert "design-integrations" in out
