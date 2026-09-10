"""`core.storage.v2_target._primary_data_home`/`effective_data_home` — #504
acceptance review round 2, H5: a worktree pane process resolving the
PRIMARY cockpit's storage root used to compare the recovered primary
DATA_HOME against THIS process's own `config.REPO_ROOT` (`storage_layout_v2`'s
dev/installed check), which is correct only evaluated inside the process
that owns that DATA_HOME. See `pane_env._apply_storage_root` for the
production fix (stamping the host's own already-resolved root into
``TAKKUB_STORAGE_ROOT``) and this module's own docstring for the fallback
heuristic used when an older host hasn't stamped it yet.
"""

from __future__ import annotations

import pytest

from agent_takkub import config, pane_env
from agent_takkub.core.storage import layout as layout_mod
from agent_takkub.core.storage.layout import storage_layout_v2 as _real_storage_layout_v2
from agent_takkub.core.storage.v2_target import _primary_data_home, effective_data_home


@pytest.fixture(autouse=True)
def _real_storage_layout_v2_default(monkeypatch):
    """tests/conftest.py's own `_isolate_runtime` (autouse) wraps
    `storage_layout_v2` so its no-arg default resolves to an isolated tmp
    dir regardless of `config.DATA_HOME` — deliberate isolation elsewhere in
    the suite, but this file's whole point is asserting exactly how the
    no-arg default resolves relative to `config.DATA_HOME`/`REPO_ROOT`.
    Re-patch back to the real function (autouse fixtures run first, so this
    wins) — same "own fixture re-patches over the isolation default"
    convention conftest.py documents for itself."""
    monkeypatch.setattr(layout_mod, "storage_layout_v2", _real_storage_layout_v2)


class TestPrimaryDataHomeFromStorageRootEnv:
    def test_uses_takkub_storage_root_verbatim_when_present(self, monkeypatch, tmp_path):
        root = tmp_path / "primary" / "v2"
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(root))
        assert _primary_data_home() == root

    def test_takkub_storage_root_wins_even_with_a_port_file_present(self, monkeypatch, tmp_path):
        root = tmp_path / "primary"  # installed shape — no /v2 suffix
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(root))
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(tmp_path / "other" / "runtime" / "port"))
        assert _primary_data_home() == root


class TestPrimaryDataHomeFallbackHeuristic:
    """No ``TAKKUB_STORAGE_ROOT`` — the older, `TAKKUB_PORT_FILE`-only path."""

    def test_no_override_returns_none(self, monkeypatch):
        monkeypatch.delenv("TAKKUB_STORAGE_ROOT", raising=False)
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        assert _primary_data_home() is None

    def test_self_referential_port_file_returns_none(self, monkeypatch, tmp_path):
        port_file = tmp_path / "runtime" / "port"
        monkeypatch.delenv("TAKKUB_STORAGE_ROOT", raising=False)
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(port_file))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", str(port_file))
        assert _primary_data_home() is None

    def test_this_process_a_dev_checkout_assumes_primary_is_also_nested(
        self, monkeypatch, tmp_path
    ):
        """#504 R2 `dev_worktree`: a worktree pane's own
        `config.DATA_HOME == config.REPO_ROOT` (it IS a dev checkout, by
        construction — a worktree can only exist while self-hosting
        agent-takkub's own development) — the only realistic scenario this
        whole divergence arises in is one where the primary is ALSO a dev
        checkout of the same repo, so the recovered primary path gets the
        nested `v2/` suffix appended."""
        primary = tmp_path / "primary"
        child = tmp_path / "child"
        monkeypatch.delenv("TAKKUB_STORAGE_ROOT", raising=False)
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(primary / "runtime" / "port"))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", "")
        monkeypatch.setattr(config, "REPO_ROOT", child)
        monkeypatch.setattr(config, "DATA_HOME", child)
        assert _primary_data_home() == primary / "v2"

    def test_this_process_not_a_dev_checkout_logs_ambiguous_and_returns_bare_path(
        self, monkeypatch, tmp_path, caplog
    ):
        primary = tmp_path / "primary"
        installed_home = tmp_path / "installed-data-home"
        monkeypatch.delenv("TAKKUB_STORAGE_ROOT", raising=False)
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(primary / "runtime" / "port"))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", "")
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "some-repo-root")
        monkeypatch.setattr(config, "DATA_HOME", installed_home)
        with caplog.at_level("WARNING"):
            result = _primary_data_home()
        assert result == primary
        assert any("storage_root_ambiguous" in r.message for r in caplog.records)


class TestEffectiveDataHomeDevWorktreeRepro:
    def test_pane_resolves_the_same_root_the_dev_primary_itself_would(self, monkeypatch, tmp_path):
        """The exact `dev_worktree` acceptance repro: a cockpit evaluated
        purely as itself (REPO_ROOT == DATA_HOME == primary) resolves
        `storage_layout_v2().root` to `primary/v2`; a child pane process,
        with only `TAKKUB_PORT_FILE` pointing at the primary, must resolve
        the SAME root via `effective_data_home(prefer_primary=True)`."""
        from agent_takkub.core.storage.layout import storage_layout_v2

        primary = tmp_path / "primary"
        child = tmp_path / "child"

        monkeypatch.setattr(config, "REPO_ROOT", primary)
        monkeypatch.setattr(config, "DATA_HOME", primary)
        cockpit_root = storage_layout_v2().root

        monkeypatch.delenv("TAKKUB_STORAGE_ROOT", raising=False)
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(primary / "runtime" / "port"))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", "")
        monkeypatch.setattr(config, "REPO_ROOT", child)
        monkeypatch.setattr(config, "DATA_HOME", child)
        pane_root = storage_layout_v2(effective_data_home(None, prefer_primary=True)).root

        assert pane_root == cockpit_root == primary / "v2"


class TestApplyStorageRootEnv:
    def test_stamps_the_hosts_own_resolved_root(self, monkeypatch, tmp_path):
        primary = tmp_path / "primary"
        monkeypatch.setattr(config, "REPO_ROOT", primary)
        monkeypatch.setattr(config, "DATA_HOME", primary)
        env: dict[str, str] = {}
        pane_env._apply_storage_root(env)
        assert env["TAKKUB_STORAGE_ROOT"] == str(primary / "v2")

    def test_build_pane_env_includes_it(self, monkeypatch, tmp_path):
        primary = tmp_path / "installed"
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "some-repo")
        monkeypatch.setattr(config, "DATA_HOME", primary)
        monkeypatch.setattr(config, "_effective_port_file_for_app", lambda: "port-file")
        env = pane_env._build_pane_env()
        assert env["TAKKUB_STORAGE_ROOT"] == str(primary)
