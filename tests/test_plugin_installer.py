"""Unit tests for the recommended dev-team plugin set + installer logic.

Covers the pure pieces (output parsing, on-disk status, missing computation) so
the 🧩 Plugins button's behaviour is verified without shelling out to claude or
launching the GUI. The subprocess install path is exercised indirectly via the
parser; the network call itself is not unit-tested.
"""

from __future__ import annotations

from agent_takkub import plugin_installer as pi


def test_recommended_set_shape():
    keys = [p.key for p in pi.RECOMMENDED]
    # The five dev-team plugins, in display order.
    assert keys == [
        "superpowers",
        "frontend-design",
        "code-review",
        "security-guidance",
        "remember",
    ]
    # Hook-heavy ones are user-only (not pushed into panes).
    by_key = {p.key: p for p in pi.RECOMMENDED}
    assert by_key["frontend-design"].pane_loaded is True
    assert by_key["code-review"].pane_loaded is True
    assert by_key["security-guidance"].pane_loaded is False
    assert by_key["remember"].pane_loaded is False


def _make_installed(cache, marketplace, plugin, version="1.0.0"):
    """Create a loadable cache layout: <mp>/<plugin>/<ver>/.claude-plugin/plugin.json."""
    d = cache / marketplace / plugin / version / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{}", encoding="utf-8")


def test_installed_on_disk(tmp_path):
    cache = tmp_path / ".claude" / "plugins" / "cache"
    # frontend-design + code-review + superpowers fully installed.
    _make_installed(cache, "claude-plugins-official", "frontend-design")
    _make_installed(cache, "claude-plugins-official", "code-review")
    _make_installed(cache, "superpowers-dev", "superpowers", "5.1.0")

    have = pi.installed_on_disk(home=tmp_path)
    assert have == {"frontend-design", "code-review", "superpowers"}

    missing = {p.key for p in pi.missing_plugins(have)}
    assert missing == {"security-guidance", "remember"}


def test_installed_on_disk_ignores_partial_install(tmp_path):
    # A plugin folder with NO version/.claude-plugin/plugin.json (interrupted
    # install) must NOT count as installed — panes wouldn't load it either.
    cache = tmp_path / ".claude" / "plugins" / "cache"
    (cache / "claude-plugins-official" / "frontend-design").mkdir(parents=True)
    assert pi.installed_on_disk(home=tmp_path) == set()


def test_ensure_marketplaces_dedupes_by_repo(monkeypatch):
    calls: list = []
    monkeypatch.setattr(pi, "_ensure_marketplace", lambda repo: calls.append(repo) or (True, "ok"))

    pi.ensure_marketplaces(list(pi.RECOMMENDED))

    # 5 plugins but only 2 distinct marketplace repos → 2 adds, not 5.
    assert calls == list(dict.fromkeys(p.marketplace_repo for p in pi.RECOMMENDED))
    assert len(calls) == 2


def test_install_plugin_success_by_exit_code(monkeypatch):
    # Exit 0 with reworded output (no "successfully installed") is still success.
    class _P:
        returncode = 0
        stdout = "Installed frontend-design"
        stderr = ""

    monkeypatch.setattr(pi, "_claude", lambda *a, **k: _P())
    ok, _msg = pi.install_plugin(pi.RECOMMENDED[1], ensure_marketplace=False)
    assert ok is True


def test_install_plugin_failure_by_exit_code(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""
        stderr = "not found in marketplace"

    monkeypatch.setattr(pi, "_claude", lambda *a, **k: _P())
    ok, msg = pi.install_plugin(pi.RECOMMENDED[1], ensure_marketplace=False)
    assert ok is False
    assert "not found" in msg


def test_missing_plugins_all_when_empty():
    missing = pi.missing_plugins(set())
    assert len(missing) == len(pi.RECOMMENDED)
