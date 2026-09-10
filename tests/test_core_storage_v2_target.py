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

from pathlib import Path

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


def _seed_v2_markers(root: Path) -> None:
    """Give *root* the minimal on-disk shape `_has_v2_layout_markers` (#504
    round 3 R3-H3) accepts as an actual storage root."""
    system = root / "system"
    system.mkdir(parents=True)
    (system / "version.json").write_text("{}")


class TestPrimaryDataHomeFromStorageRootEnv:
    def test_uses_takkub_storage_root_verbatim_when_present(self, monkeypatch, tmp_path):
        root = tmp_path / "primary" / "v2"
        _seed_v2_markers(root)
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(root))
        assert _primary_data_home() == root

    def test_takkub_storage_root_wins_even_with_a_port_file_present(self, monkeypatch, tmp_path):
        root = tmp_path / "primary"  # installed shape — no /v2 suffix
        _seed_v2_markers(root)
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(root))
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(tmp_path / "other" / "runtime" / "port"))
        assert _primary_data_home() == root

    def test_accepts_nested_v2_marker_shape(self, monkeypatch, tmp_path):
        """#504 round 4, R4-M2: a value pointing at the bare pre-nesting
        DATA_HOME (markers one level down, at ``root/v2/...``) resolves to
        the NESTED directory the markers actually live in (``root / "v2"``),
        not the container `root` itself — the container previously got
        accepted as the root outright, splitting reads/writes across two
        directories (`storage_root_bare_nested` in the round 4 fault
        harness)."""
        root = tmp_path / "primary-data-home"
        _seed_v2_markers(root / "v2")
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(root))
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        assert _primary_data_home() == root / "v2"


class TestPrimaryDataHomeStorageRootContainer:
    """#504 acceptance review round 4, R4-M2: `TAKKUB_STORAGE_ROOT` pointing
    at the CONTAINER of the real root (markers one level down at
    `<value>/v2`, not at `<value>` itself) must resolve to the nested
    directory, never the container — matching the round 4 fault harness's
    `storage_root_bare_nested` case."""

    def test_container_value_resolves_to_nested_v2_root(self, monkeypatch, tmp_path):
        container = tmp_path / "primary"
        _seed_v2_markers(container / "v2")
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(container))
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        assert _primary_data_home() == container / "v2"

    def test_markers_at_the_value_itself_win_over_a_nested_v2(self, monkeypatch, tmp_path):
        """When *both* the value and `<value>/v2` look like storage roots,
        the exact/unambiguous match at the value itself wins — this is the
        pre-existing installed-layout shape, not the container case."""
        root = tmp_path / "primary"
        _seed_v2_markers(root)
        _seed_v2_markers(root / "v2")
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(root))
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        assert _primary_data_home() == root


class TestPrimaryDataHomeStorageRootValidation:
    """#504 acceptance review round 3, R3-H3: `storage_root_wrong` /
    `storage_root_nonexistent` — an override that fails validation must
    never be used or created; it must fall back to this function's own
    per-process `TAKKUB_PORT_FILE` resolution, exactly as if
    `TAKKUB_STORAGE_ROOT` had never been set."""

    def test_nonexistent_path_falls_back_without_creating_it(self, monkeypatch, tmp_path, caplog):
        bogus = tmp_path / "does-not-exist" / "v2"
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(bogus))
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        with caplog.at_level("WARNING"):
            result = _primary_data_home()
        assert result is None
        assert not bogus.exists()
        assert any(
            "storage_root_ambiguous" in r.message and "reason=nonexistent" in r.message
            for r in caplog.records
        )

    def test_existing_dir_without_markers_falls_back(self, monkeypatch, tmp_path, caplog):
        wrong = tmp_path / "some-other-existing-dir"
        wrong.mkdir()
        (wrong / "unrelated.txt").write_text("noise")
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(wrong))
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        with caplog.at_level("WARNING"):
            result = _primary_data_home()
        assert result is None
        assert not (wrong / "system").exists()
        assert any(
            "storage_root_ambiguous" in r.message and "reason=not_a_storage_root" in r.message
            for r in caplog.records
        )

    def test_invalid_override_falls_back_to_port_file_heuristic(self, monkeypatch, tmp_path):
        """The fallback isn't just `None` — an invalid override still lets
        the older `TAKKUB_PORT_FILE` per-process resolution recover the
        real primary root."""
        wrong = tmp_path / "wrong-root"
        wrong.mkdir()
        primary = tmp_path / "primary"
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(wrong))
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(primary / "runtime" / "port"))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", "")
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "child-repo")
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "child-repo")
        assert _primary_data_home() == primary / "v2"


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
