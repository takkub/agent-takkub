"""Tests for `_apply_ecc_mute`, the helper that silences ECC's two
noisiest hooks (GateGuard fact-force and the cost-critical alerter)
in every cockpit-spawned claude session.

The helper is small but the invariants matter: dropping the mute
floods every pane with prompts the user has to hand-answer, and
clobbering a user-provided override is a footgun that's hard to
debug because env-var precedence isn't visible in the pane.
"""

from __future__ import annotations

import pytest

from agent_takkub.orchestrator import _ECC_MUTED_HOOKS, _apply_ecc_mute


@pytest.fixture(autouse=True)
def _clear_takkub_ecc_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make sure the escape hatch isn't leaking in from the host env
    # so the default-mute tests start from a clean baseline.
    monkeypatch.delenv("TAKKUB_ECC_FULL", raising=False)


class TestApplyEccMute:
    def test_sets_both_knobs_on_empty_env(self) -> None:
        env: dict[str, str] = {}
        _apply_ecc_mute(env)
        assert env["ECC_GATEGUARD"] == "off"
        assert env["ECC_DISABLED_HOOKS"] == ",".join(_ECC_MUTED_HOOKS)

    def test_disabled_hooks_lists_both_target_ids(self) -> None:
        env: dict[str, str] = {}
        _apply_ecc_mute(env)
        ids = env["ECC_DISABLED_HOOKS"].split(",")
        assert "pre:edit-write:gateguard-fact-force" in ids
        assert "post:ecc-context-monitor" in ids

    def test_preserves_user_provided_gateguard_value(self) -> None:
        # If the operator deliberately set ECC_GATEGUARD to something
        # non-default, we must not silently overwrite it. setdefault
        # semantics — first writer wins.
        env = {"ECC_GATEGUARD": "warn"}
        _apply_ecc_mute(env)
        assert env["ECC_GATEGUARD"] == "warn"

    def test_appends_to_existing_disabled_hooks(self) -> None:
        # A user-disabled hook outside our mute list must stay disabled
        # after we add our two — we append rather than replace so
        # external policy survives the cockpit's defaults.
        env = {"ECC_DISABLED_HOOKS": "post:something-else"}
        _apply_ecc_mute(env)
        assert env["ECC_DISABLED_HOOKS"].startswith("post:something-else,")
        for hook in _ECC_MUTED_HOOKS:
            assert hook in env["ECC_DISABLED_HOOKS"]

    def test_empty_disabled_hooks_string_treated_as_unset(self) -> None:
        # Bash exports an empty var as `KEY=` which arrives as `""`,
        # not as missing. Treat that as "nothing already there" so we
        # don't leave a stray leading comma.
        env = {"ECC_DISABLED_HOOKS": "   "}
        _apply_ecc_mute(env)
        assert env["ECC_DISABLED_HOOKS"] == ",".join(_ECC_MUTED_HOOKS)
        assert not env["ECC_DISABLED_HOOKS"].startswith(",")

    def test_escape_hatch_skips_mute_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # TAKKUB_ECC_FULL=1 is the documented way to opt back into
        # all ECC hooks when one of the muted ones turns out to
        # matter. The env must come back unchanged.
        monkeypatch.setenv("TAKKUB_ECC_FULL", "1")
        env: dict[str, str] = {}
        _apply_ecc_mute(env)
        assert env == {}

    def test_non_one_escape_hatch_value_still_mutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Defence against the obvious typo: only the exact literal
        # `1` opts out. Anything else (TRUE, true, on, yes) still
        # mutes, matching the documented contract.
        monkeypatch.setenv("TAKKUB_ECC_FULL", "true")
        env: dict[str, str] = {}
        _apply_ecc_mute(env)
        assert env["ECC_GATEGUARD"] == "off"
        assert ",".join(_ECC_MUTED_HOOKS) in env["ECC_DISABLED_HOOKS"]

    def test_no_return_value(self) -> None:
        # The helper mutates in place — callers in `spawn()` rely on
        # this rather than capturing a return. Pin that contract so
        # a future refactor doesn't quietly change it.
        env: dict[str, str] = {}
        result = _apply_ecc_mute(env)
        assert result is None
