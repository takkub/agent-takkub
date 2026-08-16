"""Targeted tests for #281 — an unpinned `npx` MCP must not hit the npm
registry on every pane spawn.

The cockpit pins the MCPs it ships (`@playwright/mcp@0.0.75`, …) precisely to
avoid this: npx re-resolves a dist-tag over the network every launch. A
user-added entry carries no version, so it pays that cost forever — measured on
the reporter's machine, two unpinned servers on one codex spawn took 23.1s +
9.9s, then 11.4s + 28.0s on the next run, cached and still slow.

codex pays it in the worst place: it blocks on MCP startup before accepting
input, which is the 90-150s "cold boot" every codex spawn was recorded at, and
the "pane ค้างที่ boot" behind #276. claude does not block, which is why the
same config never looked broken there.

`--prefer-offline` uses the cache and only reaches the network on a real miss.
Applied on the way into the file a pane loads — never by rewriting the master
config, which holds what the user configured and stays theirs.
"""

from __future__ import annotations

from agent_takkub.shared_dev_tools import _NPX_OFFLINE_FLAG, _prefer_offline_npx


class TestNpxEntriesGetTheFlag:
    def test_unpinned_npx_entry_is_patched(self) -> None:
        cfg = {"type": "stdio", "command": "npx", "args": ["-y", "@upstash/context7-mcp"]}
        assert _prefer_offline_npx(cfg)["args"] == [
            "-y",
            _NPX_OFFLINE_FLAG,
            "@upstash/context7-mcp",
        ]

    def test_flag_lands_before_the_package_spec(self) -> None:
        """npm only accepts its own flags ahead of the package argument — a
        flag after it would be passed through to the server instead."""
        cfg = {"command": "npx", "args": ["-y", "figma-developer-mcp", "--stdio"]}
        args = _prefer_offline_npx(cfg)["args"]
        assert args.index(_NPX_OFFLINE_FLAG) < args.index("figma-developer-mcp")
        assert args[-1] == "--stdio", "server's own flags must keep their position"

    def test_pinned_entries_are_patched_too(self) -> None:
        """Pinning skips the dist-tag lookup but not every registry check —
        and the flag is harmless when the cache already has the exact version.
        """
        cfg = {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.75"]}
        assert _NPX_OFFLINE_FLAG in _prefer_offline_npx(cfg)["args"]

    def test_entry_without_yes_flag_still_patched_at_the_front(self) -> None:
        cfg = {"command": "npx", "args": ["some-mcp"]}
        assert _prefer_offline_npx(cfg)["args"] == [_NPX_OFFLINE_FLAG, "some-mcp"]

    def test_npx_with_a_full_path_command_is_recognised(self) -> None:
        cfg = {"command": r"C:\Program Files\nodejs\npx.cmd", "args": ["-y", "some-mcp"]}
        assert _NPX_OFFLINE_FLAG in _prefer_offline_npx(cfg)["args"]

    def test_idempotent(self) -> None:
        cfg = {"command": "npx", "args": ["-y", _NPX_OFFLINE_FLAG, "some-mcp"]}
        assert _prefer_offline_npx(cfg)["args"].count(_NPX_OFFLINE_FLAG) == 1

    def test_original_config_is_not_mutated(self) -> None:
        cfg = {"command": "npx", "args": ["-y", "some-mcp"]}
        _prefer_offline_npx(cfg)
        assert cfg["args"] == ["-y", "some-mcp"], "the master config must stay untouched"


class TestNonNpxEntriesUntouched:
    def test_non_npx_command_passes_through(self) -> None:
        cfg = {"command": "graft", "args": ["mcp"]}
        assert _prefer_offline_npx(cfg) == cfg

    def test_http_entry_without_args_passes_through(self) -> None:
        cfg = {"type": "http", "url": "https://example.invalid/mcp"}
        assert _prefer_offline_npx(cfg) == cfg

    def test_malformed_entries_never_raise(self) -> None:
        assert _prefer_offline_npx({"command": "npx", "args": "not-a-list"}) == {
            "command": "npx",
            "args": "not-a-list",
        }
        assert _prefer_offline_npx({"command": "npx", "args": [1, 2]})["args"] == [1, 2]
        assert _prefer_offline_npx({"command": 42}) == {"command": 42}
        assert _prefer_offline_npx("nonsense") == "nonsense"
