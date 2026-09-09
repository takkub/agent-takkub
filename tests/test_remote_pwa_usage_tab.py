"""Structural checks for the "Usage" bottom-nav tab (per direct user
request, replacing the earlier header-chip+popup-sheet design) in the
Takkub Remote PWA (`static/index.html` + `app.js`). Two sub-tabs: "เหลือ"
(remaining quota per provider, GET /api/usage) and "ภาพรวม" (aggregate
turns/tokens per provider, GET /api/usage/history — provider totals only,
no per-account/model breakdown, no range picker/sparkline, confirmed scope).
No JS runtime in this repo's test suite — these assert the pieces exist and
are wired correctly, same spirit as other static-asset structural tests in
this project.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestIndexHtmlMarkup:
    def test_bottom_nav_has_usage_button(self):
        html = _read("index.html")
        assert 'data-view="usage"' in html

    def test_no_header_chip_or_popup_sheet(self):
        # The earlier header-icon + popup-sheet design was explicitly
        # rejected mid-task in favor of a full-page bottom-nav tab — must
        # never come back as a duplicate/competing entry point.
        html = _read("index.html")
        assert 'id="usage-chip"' not in html
        assert 'id="usage-sheet"' not in html

    def test_has_usage_view_with_two_subtabs(self):
        html = _read("index.html")
        assert 'id="view-usage"' in html
        assert 'data-usage-tab="remaining"' in html
        assert 'data-usage-tab="overview"' in html
        assert 'id="usage-tab-remaining"' in html
        assert 'id="usage-tab-overview"' in html
        assert 'id="usage-remaining-list"' in html
        assert 'id="usage-overview-list"' in html

    def test_usage_history_dead_code_stays_out(self):
        # #507's usage-history section (token table, sparkline, range
        # picker) was removed per direct user request and must not
        # reappear as part of the new "ภาพรวม" sub-tab.
        html = _read("index.html")
        assert "usage-history" not in html


class TestAppJsWiring:
    def test_switch_view_starts_and_stops_usage_polling(self):
        js = _read("app.js")
        switch_start = js.index("function switchView(name)")
        switch_end = js.index("// Pairing screen", switch_start)
        body = js[switch_start:switch_end]
        assert 'if (name === "usage") switchUsageTab(' in body
        assert "stopUsagePolling();" in body

    def test_auth_loss_stops_usage_polling(self):
        js = _read("app.js")
        for fn in ("function showPairing(errorMsg)", "function showPasswordPrompt(errorMsg)"):
            start = js.index(fn)
            end = js.index("\n  }", start)
            body = js[start:end]
            assert "stopUsagePolling();" in body

    def test_remaining_tab_reads_api_usage_only(self):
        js = _read("app.js")
        assert 'apiFetch("api/usage")' in js
        fetch_start = js.index("function fetchUsage()")
        fetch_end = js.index("\n  }", fetch_start)
        assert '"api/usage/history"' not in js[fetch_start:fetch_end]

    def test_overview_tab_reads_api_usage_history(self):
        js = _read("app.js")
        assert 'apiFetch("api/usage/history")' in js

    def test_overview_card_has_no_account_model_breakdown(self):
        # Confirmed scope: provider-level totals only, no collapsible
        # per-account/model detail — kept intentionally simpler than the
        # old #507 desktop table.
        js = _read("app.js")
        build_start = js.index("function buildUsageOverviewProviderCard(")
        build_end = js.index("\n  }", build_start)
        body = js[build_start:build_end]
        assert "usage-card-detail" not in body

    def test_usage_subtab_buttons_wired_to_switch_usage_tab(self):
        js = _read("app.js")
        assert ".usage-subtab-btn" in js
        assert "switchUsageTab(btn.dataset.usageTab)" in js
