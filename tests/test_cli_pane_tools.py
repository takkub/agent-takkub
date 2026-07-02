"""Tests for `takkub mcp ...` / `takkub plugins ...` (cli.cmd_mcp / cmd_plugins)
— the CLI surface for the per-role pane-tools policy (pane_tools_policy.py +
shared_dev_tools.py MCP master registry).

Matrix covered: read-only `list` open to every role, mutating subcommands
(allow/deny/reset/add/remove) gated lead-only, dispatch to the policy/master
functions with correct args, and regen_role_variants() firing on every
successful mutation.
"""

from __future__ import annotations

import pytest

from agent_takkub import cli, pane_tools_policy, shared_dev_tools


def _clear_role_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT"):
        monkeypatch.delenv(key, raising=False)


class TestListIsOpenToEveryone:
    def test_teammate_can_list_mcps(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )

        code = cli.main(["mcp", "list"])

        assert code == 0
        out = capsys.readouterr().out
        assert "role" in out

    def test_teammate_can_list_plugins(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_plugins",
            lambda role, default=None: default or frozenset(),
        )

        code = cli.main(["plugins", "list"])

        assert code == 0

    def test_list_filters_to_single_role(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset({"foo"})
        )

        code = cli.main(["mcp", "list", "--role", "qa"])

        assert code == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 2  # header + one role row
        assert "qa" in lines[1]

    def test_list_unknown_role_errors(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)

        code = cli.main(["mcp", "list", "--role", "nope"])

        assert code == 1
        assert "unknown role" in capsys.readouterr().out

    def test_override_role_marked_with_star(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setattr(
            pane_tools_policy, "load_policy", lambda: {"backend": {"mcps": [], "plugins": []}}
        )
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset()
        )

        code = cli.main(["mcp", "list", "--role", "backend"])

        assert code == 0
        out = capsys.readouterr().out
        assert "backend*" in out


class TestMutationsAreLeadOnly:
    @pytest.mark.parametrize(
        "argv",
        [
            ["mcp", "allow", "--role", "backend", "foo"],
            ["mcp", "deny", "--role", "backend", "foo"],
            ["mcp", "reset", "--role", "backend"],
            ["mcp", "add", "foo", "--command", "npx"],
            ["mcp", "remove", "foo"],
            ["plugins", "allow", "--role", "backend", "foo"],
            ["plugins", "deny", "--role", "backend", "foo"],
            ["plugins", "reset", "--role", "backend"],
        ],
    )
    def test_teammate_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, argv: list[str]
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        calls: list[str] = []
        monkeypatch.setattr(
            pane_tools_policy, "allow_item", lambda *a: calls.append("allow_item") or True
        )
        monkeypatch.setattr(
            pane_tools_policy, "deny_item", lambda *a: calls.append("deny_item") or True
        )
        monkeypatch.setattr(
            pane_tools_policy, "reset_role", lambda *a: calls.append("reset_role") or True
        )
        monkeypatch.setattr(
            shared_dev_tools,
            "add_mcp_server",
            lambda *a, **kw: calls.append("add_mcp_server") or True,
        )
        monkeypatch.setattr(
            shared_dev_tools,
            "remove_mcp_server",
            lambda *a: calls.append("remove_mcp_server") or True,
        )
        monkeypatch.setattr(
            shared_dev_tools, "regen_role_variants", lambda: calls.append("regen") or 0
        )

        code = cli.main(argv)

        assert code == 1
        assert calls == [], "mutation must never run for a non-lead caller"
        assert "only lead" in capsys.readouterr().out

    def test_lead_role_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(pane_tools_policy, "allow_item", lambda *a: True)
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 1)

        code = cli.main(["mcp", "allow", "--role", "backend", "foo"])

        assert code == 0

    def test_no_role_env_is_allowed_debugging_path(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setattr(pane_tools_policy, "allow_item", lambda *a: True)
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 1)

        code = cli.main(["mcp", "allow", "--role", "backend", "foo"])

        assert code == 0


class TestMcpAllowDeny:
    def test_allow_calls_policy_and_regenerates(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[tuple] = []
        monkeypatch.setattr(
            pane_tools_policy,
            "allow_item",
            lambda role, kind, name: calls.append((role, kind, name)) or True,
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        regen_calls: list[int] = []
        monkeypatch.setattr(
            shared_dev_tools, "regen_role_variants", lambda: regen_calls.append(1) or 3
        )

        code = cli.main(["mcp", "allow", "--role", "backend", "playwright"])

        assert code == 0
        assert calls == [("backend", "mcps", "playwright")]
        assert regen_calls == [1]

    def test_deny_calls_policy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[tuple] = []
        monkeypatch.setattr(
            pane_tools_policy,
            "deny_item",
            lambda role, kind, name: calls.append((role, kind, name)) or True,
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["mcp", "deny", "--role", "qa", "chrome-devtools"])

        assert code == 0
        assert calls == [("qa", "mcps", "chrome-devtools")]

    def test_allow_unknown_role_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")

        code = cli.main(["mcp", "allow", "--role", "nope", "foo"])

        assert code == 1

    def test_allow_failure_from_policy_layer_surfaces_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(pane_tools_policy, "allow_item", lambda *a: False)
        regen_calls: list[int] = []
        monkeypatch.setattr(
            shared_dev_tools, "regen_role_variants", lambda: regen_calls.append(1) or 0
        )

        code = cli.main(["mcp", "allow", "--role", "backend", "foo"])

        assert code == 1
        assert regen_calls == [], "must not regen variants when the underlying mutation failed"
        assert "could not allow" in capsys.readouterr().out


class TestMcpReset:
    def test_reset_single_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[str] = []
        monkeypatch.setattr(
            pane_tools_policy, "reset_role", lambda role: calls.append(role) or True
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["mcp", "reset", "--role", "backend"])

        assert code == 0
        assert calls == ["backend"]

    def test_reset_all_roles_uses_current_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[str] = []
        monkeypatch.setattr(
            pane_tools_policy, "reset_role", lambda role: calls.append(role) or True
        )
        monkeypatch.setattr(
            pane_tools_policy,
            "load_policy",
            lambda: {"backend": {"mcps": [], "plugins": []}, "qa": {"mcps": [], "plugins": []}},
        )
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["mcp", "reset"])

        assert code == 0
        assert calls == ["backend", "qa"]

    def test_reset_all_with_no_overrides_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[str] = []
        monkeypatch.setattr(
            pane_tools_policy, "reset_role", lambda role: calls.append(role) or True
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        regen_calls: list[int] = []
        monkeypatch.setattr(
            shared_dev_tools, "regen_role_variants", lambda: regen_calls.append(1) or 0
        )

        code = cli.main(["mcp", "reset"])

        assert code == 0
        assert calls == []
        assert regen_calls == []


class TestMcpAddRemove:
    def test_add_builds_stdio_cfg_and_splits_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        seen: dict = {}

        def _fake_add(name, cfg, force=False):
            seen["name"] = name
            seen["cfg"] = cfg
            seen["force"] = force
            return True

        monkeypatch.setattr(shared_dev_tools, "add_mcp_server", _fake_add)
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(
            ["mcp", "add", "my-mcp", "--command", "npx", "--args", "--yes some-pkg --flag"]
        )

        assert code == 0
        assert seen["name"] == "my-mcp"
        assert seen["cfg"] == {
            "type": "stdio",
            "command": "npx",
            "args": ["--yes", "some-pkg", "--flag"],
        }
        assert seen["force"] is False

    def test_add_force_flag_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        seen: dict = {}
        monkeypatch.setattr(
            shared_dev_tools,
            "add_mcp_server",
            lambda name, cfg, force=False: seen.update(force=force) or True,
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["mcp", "add", "my-mcp", "--command", "npx", "--force"])

        assert code == 0
        assert seen["force"] is True

    def test_add_blocked_by_secret_check_hints_force(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(shared_dev_tools, "add_mcp_server", lambda *a, **kw: False)
        regen_calls: list[int] = []
        monkeypatch.setattr(
            shared_dev_tools, "regen_role_variants", lambda: regen_calls.append(1) or 0
        )

        code = cli.main(["mcp", "add", "my-mcp", "--command", "npx"])

        assert code == 1
        assert regen_calls == []
        out = capsys.readouterr().out
        assert "--force" in out

    def test_remove_dispatches_and_regenerates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[str] = []
        monkeypatch.setattr(
            shared_dev_tools, "remove_mcp_server", lambda name: calls.append(name) or True
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: default or frozenset()
        )
        regen_calls: list[int] = []
        monkeypatch.setattr(
            shared_dev_tools, "regen_role_variants", lambda: regen_calls.append(1) or 0
        )

        code = cli.main(["mcp", "remove", "my-mcp"])

        assert code == 0
        assert calls == ["my-mcp"]
        assert regen_calls == [1]

    def test_remove_not_found_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(shared_dev_tools, "remove_mcp_server", lambda name: False)

        code = cli.main(["mcp", "remove", "nope"])

        assert code == 1


class TestPluginsHasNoAddRemove:
    def test_plugins_add_is_not_a_valid_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            cli.main(["plugins", "add", "foo", "--command", "npx"])


class TestPluginsAllowDenyReset:
    def test_allow_uses_plugins_kind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[tuple] = []
        monkeypatch.setattr(
            pane_tools_policy,
            "allow_item",
            lambda role, kind, name: calls.append((role, kind, name)) or True,
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_plugins",
            lambda role, default=None: default or frozenset(),
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["plugins", "allow", "--role", "critic", "pordee"])

        assert code == 0
        assert calls == [("critic", "plugins", "pordee")]

    def test_reset_single_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        calls: list[str] = []
        monkeypatch.setattr(
            pane_tools_policy, "reset_role", lambda role: calls.append(role) or True
        )
        monkeypatch.setattr(pane_tools_policy, "load_policy", lambda: {})
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_plugins",
            lambda role, default=None: default or frozenset(),
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)

        code = cli.main(["plugins", "reset", "--role", "qa"])

        assert code == 0
        assert calls == ["qa"]
