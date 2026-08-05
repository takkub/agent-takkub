"""graft_store.py — hash-keyed external graph store location (#146 follow-up).

Covers: key stability, distinctness for different paths, case-insensitivity
on Windows, manifest round-trip, and that no `decode_project_dir`-style
lossy encoding is involved (a hyphen/underscore/dot/space path must not
collide with another path that also contains those characters).
"""

from __future__ import annotations

import os

from agent_takkub import graft_store


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
    tracked `.gitignore` (found 2026-08-05). `GRAFT_STORE_ROOT` must always
    resolve under the user's home directory instead, independent of
    whatever `DATA_HOME` happens to be for this process."""
    from pathlib import Path

    from agent_takkub import config

    data_home = config.DATA_HOME.resolve()
    store_root = graft_store.GRAFT_STORE_ROOT.resolve()

    assert store_root != data_home
    assert data_home not in store_root.parents
    assert str(store_root).lower().startswith(str(Path.home().resolve()).lower())


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
