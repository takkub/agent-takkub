"""Tests for `mcp_bridge.py` (issue #100 — per-provider MCP injection adapter).

Covers the pure TOML-literal encoder plus each provider's argv translation.
Claude's path only needs a smoke test (it's a thin pass-through to the
pre-existing `shared_dev_tools.browser_profile_mcp_config_path` +
`--mcp-config`/`--strict-mcp-config`, already covered by test_browser_mcps.py);
the bulk of the new-behaviour coverage is codex's `-c` override translation.
"""

from __future__ import annotations

import json
import os

import pytest

from agent_takkub import mcp_bridge


@pytest.fixture
def isolated_mcp_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Redirect shared_dev_tools' SHARED_MCP_FILE + pane_tools_policy's
    PANE_TOOLS_POLICY_FILE to tmp paths — same isolation test_browser_mcps.py
    uses, so a real dev machine's runtime/shared-mcp.json or
    ~/.takkub/pane-tools.json never leaks into these assertions.

    `_codex_cli_version` is forced to `None` (undetectable) so every
    existing test here deterministically exercises the pre-#146
    resolve-and-disable path regardless of whether the dev machine
    running these tests happens to have a real `codex` binary on PATH —
    see TestCodexResolveVersionGate for the opposite (skip-resolve) path.
    """
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt

    target = tmp_path / "shared-mcp.json"
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", target)
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", lambda *args: [])
    monkeypatch.setattr(mcp_bridge, "_codex_cli_version", lambda *args: None)
    return target


def _write_master(path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


class TestTomlLiteral:
    def test_string_escapes_backslash_and_quote(self):
        assert mcp_bridge._toml_literal("C:\\Users\\x") == '"C:\\\\Users\\\\x"'
        assert mcp_bridge._toml_literal('say "hi"') == '"say \\"hi\\""'

    def test_list_of_strings(self):
        assert mcp_bridge._toml_literal(["-y", "@scope/pkg@1.2.3"]) == '["-y","@scope/pkg@1.2.3"]'

    def test_dict_inline_table(self):
        assert mcp_bridge._toml_literal({"FOO": "bar"}) == '{FOO="bar"}'

    def test_bool_and_number(self):
        assert mcp_bridge._toml_literal(True) == "true"
        assert mcp_bridge._toml_literal(False) == "false"
        assert mcp_bridge._toml_literal(60) == "60"

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            mcp_bridge._toml_literal(object())


class TestCodexMcpArgv:
    def test_default_codex_policy_disables_config_and_plugin_mcps(self, isolated_mcp_file):
        assert mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj") == [
            "-c",
            "mcp_servers={}",
            "-c",
            "features.plugins=false",
        ]

    def test_disables_every_resolved_inherited_server(self, isolated_mcp_file, monkeypatch):
        monkeypatch.setattr(
            mcp_bridge,
            "_codex_resolved_mcp_names",
            lambda *args: ["user_mcp", "project-mcp"],
        )

        assert mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj") == [
            "-c",
            "mcp_servers={}",
            "-c",
            "features.plugins=false",
            "-c",
            "mcp_servers.user_mcp.enabled=false",
            "-c",
            "mcp_servers.project-mcp.enabled=false",
        ]

    def test_translates_command_args_env(self, isolated_mcp_file):
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt

        _write_master(
            isolated_mcp_file,
            {
                "demo": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["-e", "1"],
                    "env": {"FOO": "1"},
                }
            },
        )
        ptp.set_role_items("codex", "mcps", ["demo"])
        sdt.regen_role_variants()
        argv = mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        pairs = list(zip(argv[0::2], argv[1::2], strict=True))
        # A non-empty explicit allowlist is still deny-by-default (the
        # regression this task closed): the disable-all-inherited prefix
        # comes first, then the role's own additive overrides.
        assert pairs == [
            ("-c", "mcp_servers={}"),
            ("-c", "features.plugins=false"),
            ("-c", 'mcp_servers.demo.command="node"'),
            ("-c", 'mcp_servers.demo.args=["-e","1"]'),
            ("-c", 'mcp_servers.demo.env={FOO="1"}'),
        ]
        # "type" has no codex equivalent — never forwarded.
        assert not any("type" in tok for tok in argv)

    def test_role_with_empty_policy_disables_all_codex_mcp_sources(self, isolated_mcp_file):
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt

        _write_master(isolated_mcp_file, {"demo": {"command": "node"}})
        ptp.set_role_items("backend", "mcps", [])  # explicit empty allowlist
        sdt.regen_role_variants()  # generates the empty shared-mcp-backend.json variant
        assert mcp_bridge.mcp_argv_for_provider("codex", "backend", None, "proj") == [
            "-c",
            "mcp_servers={}",
            "-c",
            "features.plugins=false",
        ]

    def test_propagates_resolution_failure_instead_of_swallowing(
        self, isolated_mcp_file, monkeypatch
    ):
        """Fail-closed contract: when codex's own inherited MCP set can't be
        resolved, `mcp_argv_for_provider` must raise `McpResolutionError`
        (not return a partial/empty argv) so the caller refuses to spawn
        rather than silently under-denying. See spawn_engine.py's try/except
        around this call for the caller side of the contract."""

        def _boom(*args):
            raise mcp_bridge.McpResolutionError("codex CLI timed out")

        monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", _boom)
        with pytest.raises(mcp_bridge.McpResolutionError):
            mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")

    def test_never_raises_on_corrupt_master_file(self, isolated_mcp_file):
        isolated_mcp_file.write_text("{not json", encoding="utf-8")
        # QA has a non-empty allow policy, so `_role_mcp_servers` must resolve
        # to {} on the corrupt master (not raise) while the deny-by-default
        # prefix still fires because QA has an explicit cockpit MCP policy.
        assert mcp_bridge.mcp_argv_for_provider("codex", "qa", None, "proj") == [
            "-c",
            "mcp_servers={}",
            "-c",
            "features.plugins=false",
        ]


class TestCodexCliVersion:
    """`_codex_cli_version` — best-effort `codex --version` parse used to
    gate the resolve-and-disable defense-in-depth (see
    TestCodexResolveVersionGate). Must never raise."""

    def test_parses_plain_version_output(self, monkeypatch):
        class _Result:
            stdout = "codex-cli 0.146.0\n"

        monkeypatch.setattr(mcp_bridge.subprocess, "run", lambda *a, **kw: _Result())
        assert mcp_bridge._codex_cli_version("codex", ".", {}) == (0, 146, 0)

    def test_parses_prerelease_suffix_by_leading_numbers(self, monkeypatch):
        class _Result:
            stdout = "codex-cli 0.147.0-alpha.6.3\n"

        monkeypatch.setattr(mcp_bridge.subprocess, "run", lambda *a, **kw: _Result())
        assert mcp_bridge._codex_cli_version("codex", ".", {}) == (0, 147, 0)

    def test_returns_none_on_subprocess_error(self, monkeypatch):
        import subprocess as sp

        def _boom(*a, **kw):
            raise sp.TimeoutExpired(cmd="codex", timeout=5)

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _boom)
        assert mcp_bridge._codex_cli_version("codex", ".", {}) is None

    def test_returns_none_on_unparseable_output(self, monkeypatch):
        class _Result:
            stdout = "not a version string\n"

        monkeypatch.setattr(mcp_bridge.subprocess, "run", lambda *a, **kw: _Result())
        assert mcp_bridge._codex_cli_version("codex", ".", {}) is None


class TestCodexCliVersionCached:
    """`_codex_cli_version_cached` — the per-session cache every codex-family
    spawn actually calls through (2026-08-06). Every codex spawn used to pay
    a real `codex --version` subprocess (~363ms measured) even though the
    binary's version never changes mid-session; this caches it."""

    def test_second_call_does_not_reshell_out(self, tmp_path, monkeypatch):
        fake_bin = tmp_path / "codex.cmd"
        fake_bin.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr(mcp_bridge.shutil, "which", lambda name: str(fake_bin))
        calls = []

        def _fake_run(argv, **kw):
            calls.append(argv)

            class _Result:
                stdout = "codex-cli 0.146.0\n"

            return _Result()

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _fake_run)

        first = mcp_bridge._codex_cli_version_cached(str(fake_bin), ".", {})
        second = mcp_bridge._codex_cli_version_cached(str(fake_bin), ".", {})

        assert first == (0, 146, 0)
        assert second == (0, 146, 0)
        assert len(calls) == 1, "second call must be served from cache, not re-shell out"

    def test_binary_mtime_change_invalidates_cache(self, tmp_path, monkeypatch):
        """A codex-cli upgrade in place (same path, new file) must be
        re-probed, not silently keep reporting the old version forever for
        the remainder of a long-lived cockpit session."""
        fake_bin = tmp_path / "codex.cmd"
        fake_bin.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr(mcp_bridge.shutil, "which", lambda name: str(fake_bin))
        versions = iter(["codex-cli 0.146.0\n", "codex-cli 0.150.0\n"])

        def _fake_run(argv, **kw):
            class _Result:
                stdout = next(versions)

            return _Result()

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _fake_run)

        first = mcp_bridge._codex_cli_version_cached(str(fake_bin), ".", {})
        # Simulate an in-place upgrade: same path, later mtime.
        os.utime(fake_bin, (fake_bin.stat().st_atime, fake_bin.stat().st_mtime + 10))
        second = mcp_bridge._codex_cli_version_cached(str(fake_bin), ".", {})

        assert first == (0, 146, 0)
        assert second == (0, 150, 0)

    def test_unresolvable_binary_still_caches_for_this_process(self, monkeypatch):
        monkeypatch.setattr(mcp_bridge.shutil, "which", lambda name: None)
        calls = []

        def _fake_run(argv, **kw):
            calls.append(argv)

            class _Result:
                stdout = "codex-cli 0.146.0\n"

            return _Result()

        monkeypatch.setattr(mcp_bridge.subprocess, "run", _fake_run)

        mcp_bridge._codex_cli_version_cached("codex-not-on-path", ".", {})
        mcp_bridge._codex_cli_version_cached("codex-not-on-path", ".", {})

        assert len(calls) == 1


class TestCodexMcpArgvUsesCachedVersion:
    def test_mcp_argv_for_provider_calls_cached_wrapper_not_raw(
        self, isolated_mcp_file, monkeypatch
    ):
        """Regression guard: `_codex_mcp_argv` must go through the cache,
        not call `_codex_cli_version` directly — otherwise every codex spawn
        still pays the subprocess cost this cache exists to remove."""
        calls = []
        monkeypatch.setattr(
            mcp_bridge,
            "_codex_cli_version_cached",
            lambda *a: calls.append(a) or (0, 146, 0),
        )

        mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")

        assert len(calls) == 1


class TestCodexResolveVersionGate:
    """A codex-cli at/above `_CODEX_RESOLVE_SAFE_MIN_VERSION` skips the
    resolve-and-disable defense-in-depth entirely (empirically verified
    2026-08-05 against real codex-cli 0.144.1/0.145.0/0.146.0 binaries —
    `-c mcp_servers={}` alone fully denies on all three, see the module
    docstring). Below that floor, or when the version can't be determined,
    the original conservative resolve-and-hard-abort behaviour applies
    unchanged — that's what `isolated_mcp_file` exercises by default."""

    def test_recent_version_skips_resolve_subprocess_entirely(self, isolated_mcp_file, monkeypatch):
        monkeypatch.setattr(mcp_bridge, "_codex_cli_version", lambda *a: (0, 146, 0))
        calls = []
        monkeypatch.setattr(
            mcp_bridge, "_codex_resolved_mcp_names", lambda *a: calls.append(a) or []
        )
        argv = mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        assert calls == [], "resolve subprocess must not run for a verified-safe version"
        assert argv == ["-c", "mcp_servers={}", "-c", "features.plugins=false"]

    def test_recent_version_never_hard_aborts_even_if_resolve_would_fail(
        self, isolated_mcp_file, monkeypatch
    ):
        # Prove the skip is unconditional: even a resolve function that
        # would raise never gets a chance to, because it's never called.
        monkeypatch.setattr(mcp_bridge, "_codex_cli_version", lambda *a: (0, 146, 0))

        def _boom(*args):
            raise mcp_bridge.McpResolutionError("codex CLI timed out")

        monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", _boom)
        argv = mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        assert argv == ["-c", "mcp_servers={}", "-c", "features.plugins=false"]

    def test_exact_floor_version_also_skips_resolve(self, isolated_mcp_file, monkeypatch):
        monkeypatch.setattr(
            mcp_bridge, "_codex_cli_version", lambda *a: mcp_bridge._CODEX_RESOLVE_SAFE_MIN_VERSION
        )
        calls = []
        monkeypatch.setattr(
            mcp_bridge, "_codex_resolved_mcp_names", lambda *a: calls.append(a) or []
        )
        mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        assert calls == []

    def test_older_version_still_uses_resolve_path(self, isolated_mcp_file, monkeypatch):
        monkeypatch.setattr(mcp_bridge, "_codex_cli_version", lambda *a: (0, 140, 0))
        monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", lambda *a: ["inherited_mcp"])
        argv = mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        assert "-c" in argv and "mcp_servers.inherited_mcp.enabled=false" in argv

    def test_older_version_still_hard_aborts_on_resolve_failure(
        self, isolated_mcp_file, monkeypatch
    ):
        monkeypatch.setattr(mcp_bridge, "_codex_cli_version", lambda *a: (0, 140, 0))

        def _boom(*args):
            raise mcp_bridge.McpResolutionError("codex CLI timed out")

        monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", _boom)
        with pytest.raises(mcp_bridge.McpResolutionError):
            mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")

    def test_undetectable_version_falls_back_to_resolve_path(self, isolated_mcp_file, monkeypatch):
        # isolated_mcp_file already forces this (_codex_cli_version -> None);
        # asserted explicitly here so the gate's default-conservative
        # direction has its own named test, not just an implicit fixture
        # side effect.
        monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", lambda *a: ["inherited_mcp"])
        argv = mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        assert "mcp_servers.inherited_mcp.enabled=false" in argv


class TestClaudeMcpArgv:
    def test_no_master_file_returns_empty(self, isolated_mcp_file):
        assert mcp_bridge.mcp_argv_for_provider("claude", "qa", None, "proj") == []

    def test_returns_strict_mcp_config_pair(self, isolated_mcp_file):
        from agent_takkub import shared_dev_tools as sdt

        _write_master(isolated_mcp_file, {"playwright": {"command": "npx", "args": []}})
        # QA has an explicit non-None policy, so the per-role variant must
        # exist for shared_mcp_config_path_for_role to grant it anything —
        # a policy with a missing variant now resolves to "no MCPs", not
        # the master (docs/reviews/2026-08-05-graft-mcp-security.md M1).
        sdt.regen_role_variants()
        argv = mcp_bridge.mcp_argv_for_provider("claude", "qa", None, "proj")
        assert argv[0] == "--mcp-config"
        assert argv[2] == "--strict-mcp-config"


class TestGeminiMcpArgv:
    def test_always_empty_documented_no_op(self, isolated_mcp_file):
        _write_master(isolated_mcp_file, {"playwright": {"command": "npx", "args": []}})
        assert mcp_bridge.mcp_argv_for_provider("gemini", "qa", None, "proj") == []


class TestUnknownProvider:
    def test_unknown_provider_name_returns_empty(self, isolated_mcp_file):
        assert mcp_bridge.mcp_argv_for_provider("nope", "qa", None, "proj") == []
