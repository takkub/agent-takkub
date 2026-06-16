"""M5#24: `_mint_pane_token` — the per-pane auth-token minting helper extracted
from spawn()'s four provider branches.

Invariants the four copy-pasted sites all relied on, now in one place:
  1. mints a fresh urlsafe token and stamps it into env["TAKKUB_PANE_TOKEN"];
  2. registers token → (project, role) in self._pane_tokens;
  3. REVOKES any prior token for the same (project, role) first, so a respawn
     never leaves a crashed session's old token valid;
  4. leaves OTHER (project, role) tokens untouched;
  5. lazily creates self._pane_tokens if absent.
"""

from __future__ import annotations

from agent_takkub.orchestrator import Orchestrator


class _Fake:
    """Borrows just the method under test. A plain object (not a QObject) so
    hasattr / attribute assignment work without a Qt event loop — the helper
    only touches self._pane_tokens and the secrets module."""

    _mint_pane_token = Orchestrator._mint_pane_token


def _orch() -> _Fake:
    return _Fake()


def test_mints_and_stamps_env() -> None:
    o = _orch()
    env: dict = {}
    tok = o._mint_pane_token(env, "proj", "backend")
    assert tok and isinstance(tok, str)
    assert env["TAKKUB_PANE_TOKEN"] == tok
    assert o._pane_tokens[tok] == ("proj", "backend")


def test_revokes_prior_token_for_same_pair() -> None:
    o = _orch()
    first = o._mint_pane_token({}, "proj", "backend")
    second = o._mint_pane_token({}, "proj", "backend")
    assert first != second
    assert first not in o._pane_tokens  # old token revoked
    assert o._pane_tokens[second] == ("proj", "backend")
    # exactly one live token for the pair
    live = [t for t, v in o._pane_tokens.items() if v == ("proj", "backend")]
    assert live == [second]


def test_leaves_other_pairs_intact() -> None:
    o = _orch()
    be = o._mint_pane_token({}, "proj", "backend")
    fe = o._mint_pane_token({}, "proj", "frontend")
    other_proj = o._mint_pane_token({}, "proj2", "backend")
    # re-mint backend@proj — must NOT disturb frontend@proj or backend@proj2
    be2 = o._mint_pane_token({}, "proj", "backend")
    assert be not in o._pane_tokens
    assert o._pane_tokens[fe] == ("proj", "frontend")
    assert o._pane_tokens[other_proj] == ("proj2", "backend")
    assert o._pane_tokens[be2] == ("proj", "backend")


def test_lazily_creates_registry() -> None:
    o = _orch()
    assert not hasattr(o, "_pane_tokens")
    o._mint_pane_token({}, "proj", "qa")
    assert isinstance(o._pane_tokens, dict)
