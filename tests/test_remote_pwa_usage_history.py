"""Structural checks for the Usage HISTORY section (#507) in the Takkub
Remote PWA (`static/index.html` + `app.js`). No JS runtime in this repo's
test suite — these assert the pieces exist and are wired the way the
`remote/api.py::usage_history` endpoint expects, same spirit as
`test_remote_pwa_quick_reply.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestIndexHtmlMarkup:
    def test_has_usage_history_containers(self):
        html = _read("index.html")
        for dom_id in (
            "usage-history",
            "usage-history-range",
            "usage-history-spark",
            "usage-history-cards",
            "usage-history-table",
            "usage-history-quota",
            "usage-history-uncountable",
            "usage-history-rtk",
        ):
            assert f'id="{dom_id}"' in html, dom_id

    def test_range_buttons_have_data_range_attrs(self):
        html = _read("index.html")
        for key in ("today", "week", "month"):
            assert f'data-range="{key}"' in html

    def test_history_section_sits_inside_the_usage_sheet_above_the_caption(self):
        html = _read("index.html")
        sheet_idx = html.index('id="usage-sheet"')
        history_idx = html.index('id="usage-history"')
        caption_idx = html.index('id="usage-sheet-caption"')
        assert sheet_idx < history_idx < caption_idx


class TestAppJsWiring:
    def test_fetches_the_history_endpoint(self):
        js = _read("app.js")
        assert 'apiFetch("api/usage/history' in js

    def test_history_fetch_never_joins_the_five_minute_poll_timer(self):
        """Only /api/usage (quota cards) rides USAGE_POLL_MS — the bigger
        history payload is sheet-open/range-change triggered only."""
        js = _read("app.js")
        chunk = js.split("function startUsagePolling")[1].split("function stopUsagePolling")[0]
        assert "fetchUsageHistory" not in chunk

    def test_open_sheet_triggers_a_history_fetch(self):
        js = _read("app.js")
        chunk = js.split("function openUsageSheet")[1].split("function closeUsageSheet")[0]
        assert "fetchUsageHistory()" in chunk

    def test_range_switch_wired_to_refetch(self):
        js = _read("app.js")
        assert "usageHistoryState.range = btn.getAttribute" in js
        assert "fetchUsageHistory();" in js

    def test_render_functions_defined(self):
        js = _read("app.js")
        for fn in (
            "function renderUsageHistorySpark",
            "function renderUsageHistoryCards",
            "function renderUsageHistoryTable",
            "function renderUsageHistoryQuota",
            "function renderUsageHistory",
        ):
            assert fn in js

    def test_uncountable_reason_rendered_not_dropped(self):
        js = _read("app.js")
        assert "นับไม่ได้" in js

    def test_rtk_gain_labeled_separately_never_summed(self):
        js = _read("app.js")
        chunk = js.split("function renderUsageHistory()")[1].split("function fetchUsageHistory")[0]
        assert "rtk_gain" in chunk
        assert "ไม่รวมกับตัวเลขข้างบน" in chunk


class TestServiceWorkerCacheBumped:
    def test_cache_version_bumped_for_this_appjs_change(self):
        """#507 changed app.js/index.html — sw.js's cache-first shell must
        be bumped or a phone keeps serving the pre-#507 bundle forever
        (SW cache-first, see pwa-appjs-change-must-bump-sw-cache learnings)."""
        js = _read("sw.js")
        m = re.search(r'CACHE_NAME = "takkub-remote-shell-v(\d+)"', js)
        assert m is not None
        assert int(m.group(1)) >= 38
