"""Structural checks for the team-preset picker (#512) in the Takkub Remote
PWA (`static/index.html` + `app.js`). No JS runtime in this repo's test
suite — these assert the pieces exist and are wired the way the
`remote/api.py::team_preset_status`/`team_preset_set` endpoints expect, same
spirit as `test_remote_pwa_usage_history.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestIndexHtmlMarkup:
    def test_has_team_chip_and_sheet_containers(self):
        html = _read("index.html")
        for dom_id in (
            "team-chip",
            "team-sheet",
            "team-sheet-project",
            "team-sheet-close",
            "team-sheet-override",
            "team-sheet-list",
        ):
            assert f'id="{dom_id}"' in html, dom_id

    def test_team_chip_sits_before_usage_chip_in_header(self):
        """Item 6's mockup groups the team switch with the other project-
        scoped header actions, ahead of the account-wide usage chip."""
        html = _read("index.html")
        assert html.index('id="team-chip"') < html.index('id="usage-chip"')


class TestAppJsWiring:
    def test_fetches_and_posts_the_team_preset_endpoint(self):
        js = _read("app.js")
        assert 'apiFetch("api/team-preset"' in js
        assert '"method": "POST"' in js or 'method: "POST"' in js

    def test_open_sheet_triggers_a_fetch(self):
        js = _read("app.js")
        chunk = js.split("function openTeamSheet")[1].split("function closeTeamSheet")[0]
        assert "fetchTeamPreset()" in chunk

    def test_render_functions_defined(self):
        js = _read("app.js")
        for fn in (
            "function renderTeamChip",
            "function renderTeamSheet",
            "function fetchTeamPreset",
            "function setTeamPreset",
        ):
            assert fn in js

    def test_rows_only_clickable_in_control_mode(self):
        """Read-only phones (view mode) must not even attempt the POST —
        same pattern as the composer/close-project button elsewhere."""
        js = _read("app.js")
        chunk = js.split("function renderTeamSheet")[1].split("function fetchTeamPreset")[0]
        assert 'state.mode === "control"' in chunk
        assert "setTeamPreset(opt.id)" in chunk

    def test_override_line_shown_only_when_active(self):
        js = _read("app.js")
        chunk = js.split("function renderTeamSheet")[1].split("function fetchTeamPreset")[0]
        assert "data.override" in chunk
        assert "overrideEl.hidden = false" in chunk
        assert "overrideEl.hidden = true" in chunk

    def test_project_switch_refreshes_team_preset(self):
        """`selectProject` (tapping a different open project) must refetch —
        team preset is per-project, unlike the account-wide usage chip."""
        js = _read("app.js")
        chunk = js.split("function selectProject")[1].split("function renderProjects")[0]
        assert "fetchTeamPreset()" in chunk

    def test_initial_boot_fetches_team_preset(self):
        js = _read("app.js")
        chunk = js.split("function enterAuthenticatedApp")[1].split("function init()")[0]
        assert "fetchTeamPreset" in chunk


class TestServiceWorkerCacheBumped:
    def test_cache_version_bumped_for_this_appjs_change(self):
        """#512 changed app.js/index.html — sw.js's cache-first shell must
        be bumped or a phone keeps serving the pre-#512 bundle forever
        (SW cache-first, see pwa-appjs-change-must-bump-sw-cache learnings)."""
        js = _read("sw.js")
        m = re.search(r'CACHE_NAME = "takkub-remote-shell-v(\d+)"', js)
        assert m is not None
        assert int(m.group(1)) >= 39
