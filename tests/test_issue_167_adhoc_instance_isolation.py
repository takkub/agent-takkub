"""Issue #167 — ad-hoc `qa#N` instances (spawned by literally typing
`--role qa#2`, NOT via `assign --shards N`) must get the same browser
isolation a real `--shards` fan-out gets, even though the ad-hoc path never
sets `shard_total` (see `cmd_assign` in cli.py: `--role qa#2` falls through
to the plain single-assign branch with no `shard_total` field at all).

The isolation machinery (`_split_shard`, `should_manage_native_chrome`,
`pane_guard.classify`, `browser_profile_mcp_config_path`) all key off the
INT parsed out of the pane key's `#N` suffix, never off `shard_total` — so
this test proves that chain end-to-end for a pane key that never went
through a real fan-out, closing the gap #167 originally reported (mb
hard-codes CDP 9222; two unisolated browser panes collided on it).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import browser_chrome, pane_guard
from agent_takkub import shared_dev_tools as sdt
from agent_takkub.pipeline_executor import _split_shard
from agent_takkub.shared_dev_tools import (
    browser_profile_mcp_config_path,
    ensure_browser_mcps,
)


@pytest.fixture
def isolated_mcp_file(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    from agent_takkub import pane_tools_policy as ptp

    target = tmp_path / "shared-mcp.json"
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", target)
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    return target


class TestAdHocInstanceSuffixIsolatedLikeARealShard:
    def test_split_shard_treats_manually_typed_suffix_like_a_fanout_shard(self) -> None:
        # This is the ONLY thing that ever produces shard_idx downstream —
        # it reads the pane key itself, not any shard_total metadata.
        assert _split_shard("qa#2") == ("qa", 2)

    def test_native_chrome_declined_for_ad_hoc_instance_same_as_real_shard(self) -> None:
        base_role, shard_idx = _split_shard("qa#2")
        assert not browser_chrome.should_manage_native_chrome(
            base_role, shard_idx, platform="win32"
        )

    def test_pane_guard_denies_mb_for_ad_hoc_instance_suffix(self) -> None:
        # role passed to pane_guard.classify is the raw pane key ("qa#2"),
        # not the base_role — the guard's own "#" in raw_role check does not
        # require shard_total either; it fires on the suffix alone.
        verdict = pane_guard.classify("mb go http://localhost:3000", "qa#2")
        assert not verdict.allowed
        assert verdict.rule == "browser_driver:mb-shard-cdp-9222"

    def test_first_unsuffixed_instance_still_allowed_mb(self) -> None:
        # The FIRST instance ("qa", no suffix) is a normal single pane and
        # keeps using mb/native-Chrome exactly as before — only the second+
        # ad-hoc instance is forced onto isolated Playwright MCP profiles.
        assert pane_guard.classify("mb go http://localhost:3000", "qa").allowed
        assert browser_chrome.should_manage_native_chrome("qa", None, platform="win32")

    def test_browser_profile_isolated_for_ad_hoc_instance_without_shard_total(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # browser_profile_mcp_config_path has no shard_total parameter at
        # all — it only ever sees the int _split_shard already extracted.
        # An ad-hoc "qa#2" and a real fan-out shard #2 are indistinguishable
        # to it, so both get isolated Playwright/chrome-devtools profiles.
        ensure_browser_mcps()
        base_role, shard_idx = _split_shard("qa#2")
        path = browser_profile_mcp_config_path(base_role, shard_idx, "proj_a")
        assert path is not None
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        udd = data["mcpServers"]["playwright"]["args"]
        idx = udd.index("--user-data-dir")
        profile_dir = pathlib.Path(udd[idx + 1])
        assert "shard2" in profile_dir.name  # isolated, not the master profile

    def test_ad_hoc_instance_and_unsuffixed_first_pane_get_distinct_profiles(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ensure_browser_mcps()
        first_path = browser_profile_mcp_config_path("qa", None, "proj_a")
        second_role, second_idx = _split_shard("qa#2")
        second_path = browser_profile_mcp_config_path(second_role, second_idx, "proj_a")

        def _udd(cfg_path: str) -> str:
            data = json.loads(pathlib.Path(cfg_path).read_text(encoding="utf-8"))
            args = data["mcpServers"]["playwright"]["args"]
            return args[args.index("--user-data-dir") + 1]

        assert _udd(first_path) != _udd(second_path)
