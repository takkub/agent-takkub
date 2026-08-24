"""core.resilience.doctor_section — `takkub doctor`'s [resilience] rows.
Reads `circuit_breaker.load_persisted_state()` (a separate short-lived
process, same as `core.context_sources.doctor_section`), so every test here
drives it purely through the on-disk snapshot, not the in-process registry."""

from __future__ import annotations

import json

import pytest

from agent_takkub.core.resilience.doctor_section import build_findings
from agent_takkub.core.resilience.policy import POLICY


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, monkeypatch):
    from agent_takkub import config

    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    yield tmp_path


def _write_state(tmp_path, payload: dict) -> None:
    from agent_takkub import config

    path = config.DATA_HOME / "resilience" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestBuildFindings:
    def test_one_row_per_policy_entry(self, _isolated_data_home) -> None:
        rows = build_findings()
        assert {r.name for r in rows} == set(POLICY.keys())
        assert all(r.category == "resilience" for r in rows)

    def test_non_breaker_entry_is_info_and_names_fallback(self, _isolated_data_home) -> None:
        rows = {r.name: r for r in build_findings()}
        git_row = rows["git"]
        assert git_row.status == "info"
        assert "Explorer files still usable" in git_row.detail
        assert "not breaker-wired" in git_row.detail

    def test_breaker_entry_with_no_recorded_calls_is_info(self, _isolated_data_home) -> None:
        rows = {r.name: r for r in build_findings()}
        assert rows["figma"].status == "info"
        assert "no calls recorded yet" in rows["figma"].detail

    def test_breaker_entry_closed_state_is_ok(self, _isolated_data_home) -> None:
        _write_state(
            _isolated_data_home,
            {"figma": {"state": "closed", "failure_count": 0, "failure_threshold": 3}},
        )
        rows = {r.name: r for r in build_findings()}
        assert rows["figma"].status == "ok"
        assert "state=closed" in rows["figma"].detail

    def test_breaker_entry_open_state_is_warn(self, _isolated_data_home) -> None:
        _write_state(
            _isolated_data_home,
            {
                "figma": {
                    "state": "open",
                    "failure_count": 3,
                    "failure_threshold": 3,
                    "open_until": 12345.0,
                }
            },
        )
        rows = {r.name: r for r in build_findings()}
        assert rows["figma"].status == "warn"
        assert "failures=3/3" in rows["figma"].detail
        assert "open_until=" in rows["figma"].detail

    def test_breaker_entry_half_open_state_is_info(self, _isolated_data_home) -> None:
        _write_state(
            _isolated_data_home,
            {"figma": {"state": "half_open", "failure_count": 3, "failure_threshold": 3}},
        )
        rows = {r.name: r for r in build_findings()}
        assert rows["figma"].status == "info"
