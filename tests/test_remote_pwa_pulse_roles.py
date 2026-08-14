"""Structural checks for the #200 fix — the Pulse page must render every
open teammate pane (working *and* idle), not just a bare count. No JS
runtime in this repo's test suite (same spirit as test_remote_pwa_scroll_pin.py)
— these assert the PWA source actually reads each role's `state` instead of
hard-coding `idle=false`, and that the stale "count only" copy is gone.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestRenderPulseShowsTeamState:
    def test_role_chip_reads_state_from_server_not_hardcoded_false(self):
        js = _read("app.js")
        render_start = js.index("function renderPulse(projects)")
        render_end = js.index("function fmtPct(", render_start)
        body = js[render_start:render_end]
        # Pre-#200 this was a hardcoded `false` — every teammate chip always
        # rendered as "working" because `roles` only ever contained working
        # panes. Now the server sends idle panes too, so the chip must read
        # the per-role state instead of assuming.
        assert 'makeRoleChip(r.role, r.runtime_sec, r.state !== "working", r.provider)' in body
        assert "makeRoleChip(r.role, r.runtime_sec, false, r.provider)" not in body

    def test_visible_count_no_longer_requires_working_roles(self):
        js = _read("app.js")
        render_start = js.index("function renderPulse(projects)")
        render_end = js.index("function fmtPct(", render_start)
        body = js[render_start:render_end]
        # `visible` must count a project as long as it has *any* role (idle
        # included) or a lead entry — not only ones the old roleCount-as-
        # working-count conflation implied.
        assert "if (p.lead || roleCount) visible += 1;" in body

    def test_empty_state_copy_no_longer_implies_working_only(self):
        js = _read("app.js")
        assert "ไม่มีงานกำลังรันอยู่" not in js
        assert "ไม่มี pane เปิดอยู่" in js


class TestPulseSubtitleAndCaptionReflectTeamVisibility:
    def test_view_subtitle_no_longer_claims_count_only(self):
        js = _read("app.js")
        assert '"เห็นแค่จำนวน"' not in js

    def test_caption_no_longer_claims_task_identity_is_hidden(self):
        html = _read("index.html")
        # Old copy: "ไม่แสดงว่าใครทำ task อะไร — ดูรายละเอียดที่หน้า Lead" —
        # now false, since #200 shows role + state per pane.
        assert 'id="pulse-caption">ไม่แสดงว่าใครทำ task อะไร' not in html
        caption_idx = html.index('id="pulse-caption"')
        caption_end = html.index("</div>", caption_idx)
        caption = html[caption_idx:caption_end]
        assert "หน้า Lead" in caption  # still points to Lead for full task detail
