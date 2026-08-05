"""graft_store.py — hash-keyed external graph store location (#146 follow-up).

Covers: key stability, distinctness for different paths, case-insensitivity
on Windows, manifest round-trip, and that no `decode_project_dir`-style
lossy encoding is involved (a hyphen/underscore/dot/space path must not
collide with another path that also contains those characters).
"""

from __future__ import annotations

import os

from agent_takkub import graft_store

# Captured at module-import time — i.e. during pytest COLLECTION, which runs
# for every test file before any test's fixtures execute. This is the real,
# un-patched `GRAFT_STORE_ROOT` production computed at actual module import.
# conftest.py's autouse `_isolate_runtime` fixture monkeypatches
# `graft_store.GRAFT_STORE_ROOT` to a per-test tmp dir at the START of every
# test (belt-and-suspenders isolation so no test can ever write into the
# real `~/.agent-takkub`) — by the time any test BODY runs,
# `graft_store.GRAFT_STORE_ROOT` is already that tmp override, not the real
# value. Reading the module attribute from inside a test therefore only
# proves the tmp override sits under `Path.home()`, which is true by
# construction of the fixture and never actually exercises the formula
# `graft_store.py` really ships. This constant sidesteps that by keeping the
# value observed before any fixture ever ran.
_REAL_GRAFT_STORE_ROOT = graft_store.GRAFT_STORE_ROOT


def test_graph_key_is_stable_across_calls(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()

    assert graft_store.graph_key(d) == graft_store.graph_key(d)


def test_graph_key_differs_for_different_paths(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    assert graft_store.graph_key(a) != graft_store.graph_key(b)


def test_graph_key_not_derived_from_lossy_name_encoding(tmp_path):
    """Two visually-similar names that a naive hyphen/underscore-mangling
    encoder could collide on must still hash distinctly — proves the key
    is a real path hash, not a `decode_project_dir`-style transform."""
    a = tmp_path / "my-project.web"
    b = tmp_path / "my_project_web"
    a.mkdir()
    b.mkdir()

    assert graft_store.graph_key(a) != graft_store.graph_key(b)


def test_graph_store_dir_lives_outside_target(tmp_path, monkeypatch):
    monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "_store_root")
    target = tmp_path / "target"
    target.mkdir()

    store = graft_store.graph_store_dir(target)

    assert str(store) != str(target)
    assert not str(store).startswith(str(target))
    assert str(graft_store.GRAFT_STORE_ROOT) in str(store)


def test_staging_dir_for_lives_outside_target_and_store(tmp_path, monkeypatch):
    """H1 follow-up (2026-08-06): the staging mirror must be a TRUE SIBLING
    of both *target* and the graph store — nested inside either reproduces
    the self-ignoring-.gitignore bug `_graft_store_base`'s docstring already
    documents for a store nested inside its own target."""
    monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "_store_root")
    monkeypatch.setattr(graft_store, "GRAFT_STAGING_ROOT", tmp_path / "_staging_root")
    target = tmp_path / "target"
    target.mkdir()

    store = graft_store.graph_store_dir(target)
    staging = graft_store.staging_dir_for(target)

    assert str(staging) != str(target)
    assert not str(staging).startswith(str(target))
    assert str(staging) != str(store)
    assert not str(staging).startswith(str(store))
    assert not str(store).startswith(str(staging))
    assert str(graft_store.GRAFT_STAGING_ROOT) in str(staging)


def test_staging_dir_for_shares_graph_key_with_store(tmp_path, monkeypatch):
    """Store and staging are paired by name (`graph_key`) so callers can
    find one from the other with no extra bookkeeping (disk_usage.py's
    scan/prune folds staging bytes into the matching store entry this way)."""
    monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "_store_root")
    monkeypatch.setattr(graft_store, "GRAFT_STAGING_ROOT", tmp_path / "_staging_root")
    target = tmp_path / "target"
    target.mkdir()

    assert graft_store.staging_dir_for(target).name == graft_store.graph_store_dir(target).name


def test_write_and_read_store_manifest_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "_store_root")
    target = tmp_path / "target"
    target.mkdir()

    graft_store.write_store_manifest(target)
    store = graft_store.graph_store_dir(target)

    assert graft_store.read_store_manifest(store) == str(target.resolve())


def test_read_store_manifest_missing_returns_none(tmp_path):
    assert graft_store.read_store_manifest(tmp_path / "no-such-store") is None


def test_iter_store_dirs_lists_only_directories(tmp_path, monkeypatch):
    root = tmp_path / "_store_root"
    root.mkdir()
    (root / "store-a").mkdir()
    (root / "store-b").mkdir()
    (root / "stray-file.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", root)

    dirs = {p.name for p in graft_store.iter_store_dirs()}

    assert dirs == {"store-a", "store-b"}


def test_iter_store_dirs_empty_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "does-not-exist")

    assert graft_store.iter_store_dirs() == []


def test_instance_key_stable_for_same_data_home(tmp_path):
    a = tmp_path / "a"
    a.mkdir()

    assert graft_store._instance_key(a) == graft_store._instance_key(a)


def test_instance_key_differs_for_different_data_homes(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    assert graft_store._instance_key(a) != graft_store._instance_key(b)


def test_graft_store_root_never_under_data_home():
    """The actual #146-follow-up regression this module fixes: a dev
    checkout has ``DATA_HOME == REPO_ROOT`` (config.py's
    ``_resolve_data_home``), and the cockpit's own repo is routinely also a
    configured project — a DATA_HOME-relative store would then land INSIDE
    that project's own target tree, which is exactly how `graft build`
    ended up appending a `graft-graphs/<hash>/` line to this repo's own
    tracked `.gitignore` (found 2026-08-05). This machine (and CI, which
    always runs the suite from a dev checkout) hits exactly that
    ``DATA_HOME == REPO_ROOT`` case, so `GRAFT_STORE_ROOT` must fall back to
    the user's home directory here — see `test_graft_store_base_*` below
    for the M5 fix's OTHER branch (an installed build, or an explicit
    ``AGENT_TAKKUB_HOME``, where `GRAFT_STORE_ROOT` is DATA_HOME-relative
    on purpose).

    Uses `_REAL_GRAFT_STORE_ROOT` (captured pre-fixture, see module top),
    NOT `graft_store.GRAFT_STORE_ROOT` — the latter is a tmp-dir stand-in by
    the time this test body runs (conftest's autouse isolation fixture), and
    on Windows that tmp dir happens to also sit under `Path.home()`
    (`%LOCALAPPDATA%\\Temp\\...`), so asserting against it would pass this
    test even if production's real formula regressed to be DATA_HOME-based —
    exactly the bug this test exists to catch.
    """
    from pathlib import Path

    from agent_takkub import config

    data_home = config.DATA_HOME.resolve()
    store_root = _REAL_GRAFT_STORE_ROOT.resolve()

    assert store_root != data_home
    assert data_home not in store_root.parents

    # Same case-folding rule as `graft_store._normalize_for_key`: NTFS is
    # case-insensitive so Windows paths may differ only in casing, but on a
    # case-sensitive filesystem (Linux/macOS) casing is significant and
    # blanket-lowercasing would mask a genuine mismatch.
    home_str = str(Path.home().resolve())
    store_str = str(store_root)
    if os.name == "nt":
        home_str = home_str.lower()
        store_str = store_str.lower()
    assert store_str.startswith(home_str)


# ── M5: GRAFT_STORE_ROOT honours AGENT_TAKKUB_HOME ───────────────────────


def test_graft_store_base_is_data_home_when_not_repo_root(tmp_path, monkeypatch):
    """M5: an installed build (or an explicit `AGENT_TAKKUB_HOME`) — where
    `DATA_HOME != REPO_ROOT` — must root the store under DATA_HOME, not a
    hard-coded `Path.home()`, so a user who relocated cockpit data off the
    boot drive gets graft's multi-GB stores (H1) moved with it."""
    custom_home = tmp_path / "custom-data-home"
    monkeypatch.setattr(graft_store, "DATA_HOME", custom_home)
    monkeypatch.setattr(graft_store, "REPO_ROOT", tmp_path / "unrelated-repo-root")

    assert graft_store._graft_store_base() == custom_home


def test_graft_store_base_falls_back_to_home_when_data_home_is_repo_root(tmp_path, monkeypatch):
    """The one exception (see module docstring): DATA_HOME == REPO_ROOT (a
    dev checkout) would nest the store inside the cockpit's own repo, so
    that case alone still falls back to `Path.home() / ".agent-takkub"`."""
    from pathlib import Path

    same_dir = tmp_path / "dev-checkout"
    monkeypatch.setattr(graft_store, "DATA_HOME", same_dir)
    monkeypatch.setattr(graft_store, "REPO_ROOT", same_dir)

    assert graft_store._graft_store_base() == Path.home() / ".agent-takkub"


if os.name == "nt":

    def test_graph_key_case_insensitive_on_windows(tmp_path):
        d = tmp_path / "MixedCase"
        d.mkdir()
        lower = str(d).lower()
        upper = str(d).upper()

        # Both resolve to the SAME real dir on Windows' case-insensitive FS —
        # the key must fold to one entry, not silently split the graph in two.
        from pathlib import Path

        assert graft_store.graph_key(Path(lower)) == graft_store.graph_key(Path(upper))
