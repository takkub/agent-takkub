"""Structural checks for #517 (mobile showed "now" on every history/live
message instead of the record's real time). No JS runtime in this repo's
test suite — these assert app.js reads `ts` from the server-sent record
instead of stamping render time, same spirit as `test_remote_pwa_resume.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestAppJsReadsRecordTimestamp:
    def test_time_label_dead_code_is_removed(self):
        """The old `timeLabel()` always returned `new Date()` — the whole
        point of the fix is that nothing renders wall-clock-at-draw-time
        any more."""
        js = _read("app.js")
        assert "function timeLabel(" not in js

    def test_format_ts_exists_and_falls_back_to_now_only_when_missing(self):
        js = _read("app.js")
        assert "function formatTs(ts)" in js
        chunk = js.split("function formatTs(ts)")[1].split("\n\n")[0]
        # A present, finite ts renders from it; only a missing/invalid one
        # (no timestamp source for that provider) reads the live clock.
        assert 'typeof ts === "number"' in chunk
        assert "new Date(ts * 1000)" in chunk

    def test_append_msg_dom_takes_ts_and_uses_format_ts_for_who_and_done_chip(self):
        js = _read("app.js")
        assert "function appendMsgDom(kind, text, ts, skipScroll)" in js
        chunk = js.split("function appendMsgDom(kind, text, ts, skipScroll)")[1].split(
            "function appendProjectMessage"
        )[0]
        # Both the reply header's time and the done chip's time must read
        # from the record's own ts, not re-derive "now".
        assert chunk.count("formatTs(ts)") == 2
        assert "new Date()" not in chunk

    def test_parse_sse_ts_reads_the_servers_stamped_ts(self):
        js = _read("app.js")
        assert "function parseSseTs(raw)" in js
        chunk = js.split("function parseSseTs(raw)")[1].split("\n\n")[0]
        assert "payload.ts" in chunk

    def test_live_lead_and_user_and_done_events_forward_parse_sse_ts(self):
        js = _read("app.js")
        assert (
            "appendLeadLive(parseSseData(evt.data, project), project, parseSseTs(evt.data))" in js
        )
        assert 'appendProjectMessage(project, "me", text, parseSseTs(evt.data))' in js
        assert (
            'appendProjectMessage(project, "done", parseSseData(evt.data, project), '
            "parseSseTs(evt.data))" in js
        )

    def test_history_replay_carries_the_records_ts_through_to_the_dom(self):
        """loadHistory stores each server record's `ts`, and
        renderSelectedProject's replay loop must hand it to appendMsgDom —
        this is the actual #517 fix: history no longer renders every
        message as "just now"."""
        js = _read("app.js")
        chunk = js.split("messages.forEach(function (m) {")[1].split("        pending.forEach")[0]
        assert "m.ts" in chunk
        assert "lead.messages.push({ kind: historyKind(m), text: text, ts: ts });" in chunk
        assert "appendMsgDom(message.kind, message.text, message.ts, true)" in js

    def test_resume_snapshot_also_carries_ts(self):
        js = _read("app.js")
        chunk = js.split("resumedMessages.forEach")[1].split("resumedLead.historyLoaded")[0]
        assert "message.ts" in chunk


class TestServiceWorkerCacheBumpedForThisChange:
    def test_cache_version_bumped(self):
        js = _read("sw.js")
        m = re.search(r'CACHE_NAME = "takkub-remote-shell-v(\d+)"', js)
        assert m is not None
        assert int(m.group(1)) >= 40
