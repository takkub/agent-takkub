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


def test_parse_installed_keys():
    stdout = (
        "Installed plugins:\n"
        "\n"
        "  ❯ code-review@claude-plugins-official\n"
        "    Version: cd3ca5bd4a4b\n"
        "    Scope: user\n"
        "    Status: ✔ enabled\n"
        "\n"
        "  ❯ superpowers@superpowers-dev\n"
        "    Version: 5.1.0\n"
    )
    assert pi.parse_installed_keys(stdout) == {"code-review", "superpowers"}


def test_parse_installed_keys_empty():
    assert pi.parse_installed_keys("Installed plugins:\n\n") == set()


def test_installed_on_disk(tmp_path):
    cache = tmp_path / ".claude" / "plugins" / "cache"
    # frontend-design + code-review present; the rest absent.
    (cache / "claude-plugins-official" / "frontend-design").mkdir(parents=True)
    (cache / "claude-plugins-official" / "code-review").mkdir(parents=True)
    (cache / "superpowers-dev" / "superpowers").mkdir(parents=True)

    have = pi.installed_on_disk(home=tmp_path)
    assert have == {"frontend-design", "code-review", "superpowers"}

    missing = {p.key for p in pi.missing_plugins(have)}
    assert missing == {"security-guidance", "remember"}


def test_missing_plugins_all_when_empty():
    missing = pi.missing_plugins(set())
    assert len(missing) == len(pi.RECOMMENDED)
