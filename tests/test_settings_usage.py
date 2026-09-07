"""Widget smoke tests for the Usage settings view (issue #507).

Offscreen QPA (session-scoped QApplication from tests/conftest.py) — same
"tofu" widget-property style as test_settings_window.py. `config.RUNTIME_DIR`
is isolated per test by that file's own autouse fixture (usage_ledger reads
it dynamically, so it picks up the isolation automatically).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import settings_window, usage_ledger
from agent_takkub.settings_usage import _range_query_kwargs


@pytest.fixture(autouse=True)
def _isolate_settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_takkub import (
        config,
        custom_roles,
        pane_tools_policy,
        performance_settings,
        pipeline_config,
        provider_state,
        role_models,
        shared_dev_tools,
        skill_policy,
        user_profile,
    )

    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pipeline_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", tmp_path / "default-claude-config")
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.setattr(performance_settings, "path", lambda: tmp_path / "performance.json")
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")


def _open_usage_view():
    return settings_window.SettingsWindow(initial_view=settings_window.VIEW_USAGE)


class TestUsageViewSmoke:
    def test_navigates_to_usage_without_crashing_on_empty_ledger(self):
        dlg = _open_usage_view()
        assert dlg._stack.currentIndex() == settings_window.VIEW_USAGE
        assert "Refresh" in dlg._usage_status_label.text() or dlg._usage_status_label.text()
        dlg.deleteLater()

    def test_renders_countable_provider_row_and_total(self):
        usage_ledger.record_turn(
            "claude",
            "default",
            "2026-09-05T10:00:00Z",
            "r1",
            "claude-sonnet-5",
            {"input": 10, "cache_creation": 20, "cache_read": 30, "output": 40},
        )
        dlg = _open_usage_view()
        # header row (9 cols) + one data row (9 cols) = 18 grid items.
        assert dlg._usage_table_grid.count() == 18
        dlg.deleteLater()

    def test_renders_uncountable_provider_reason_not_a_fabricated_row(self):
        usage_ledger.account_dir("gemini", "default").mkdir(parents=True, exist_ok=True)
        dlg = _open_usage_view()
        assert "gemini" in dlg._usage_uncountable_label.text()
        assert "นับไม่ได้" in dlg._usage_uncountable_label.text()
        dlg.deleteLater()

    def test_construction_never_touches_import_all(self, monkeypatch):
        """Building the view must be cheap (no transcript scan) — only the
        explicit Refresh click may call the expensive import."""
        calls = []
        monkeypatch.setattr(usage_ledger, "import_all", lambda *a, **k: calls.append(1) or {})
        dlg = _open_usage_view()
        assert calls == []
        dlg.deleteLater()

    def test_range_combo_switches_query_window(self, monkeypatch):
        seen: list[dict] = []
        real_query = usage_ledger.query_usage

        def spy(*, provider=None, **kwargs):
            seen.append(kwargs)
            return real_query(provider=provider, **kwargs)

        monkeypatch.setattr(usage_ledger, "query_usage", spy)
        dlg = _open_usage_view()
        seen.clear()
        dlg._usage_range_combo.setCurrentIndex(0)  # "today"
        assert seen and seen[-1] == {"days": 1}
        dlg.deleteLater()

    def test_refresh_click_starts_background_thread_not_main_thread_call(self, monkeypatch):
        """The expensive import must run off the calling (main) thread."""
        import threading

        seen_threads: list[int] = []

        def fake_import_all():
            seen_threads.append(threading.get_ident())
            return {}

        monkeypatch.setattr(usage_ledger, "import_all", fake_import_all)
        dlg = _open_usage_view()
        main_thread_id = threading.get_ident()
        dlg._on_usage_refresh_clicked()
        dlg._usage_import_thread.wait(5000)
        assert seen_threads, "import_all was never called"
        assert seen_threads[0] != main_thread_id
        dlg.deleteLater()


class TestRangeQueryKwargs:
    def test_today_maps_to_one_day(self):
        assert _range_query_kwargs("today") == {"days": 1}

    def test_week_maps_to_seven_days(self):
        assert _range_query_kwargs("week") == {"days": 7}

    def test_month_maps_to_current_month(self):
        from datetime import UTC, datetime

        expected = datetime.now(tz=UTC).strftime("%Y-%m")
        assert _range_query_kwargs("month") == {"month": expected}
