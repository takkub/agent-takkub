"""2026-11-10 audit: successful remote (phone/PWA) requests left no trace in
events.log — only rejections did — so a usage audit read the phone as unused
while the owner sent 558 messages from it in 90 days. Every authenticated
request now leaves a `remote_use` breadcrumb: POST (a user action) always,
read-only GET polling at most once per route per interval."""

from __future__ import annotations

import json

from agent_takkub import config
from agent_takkub.remote import http_server


def _events(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_post_always_logged_get_throttled_and_query_never_logged(tmp_path, monkeypatch):
    log = tmp_path / "events.log"
    monkeypatch.setattr(config, "EVENTS_LOG", log)
    monkeypatch.setattr(http_server, "_use_log_last", {})
    monkeypatch.setattr(http_server, "_use_log_window", [0.0, 0.0])

    for _ in range(3):
        http_server._log_use("POST", "/s3cr3t/api/lead/say")
    for _ in range(5):
        http_server._log_use("GET", "/s3cr3t/api/lead/history?ticket=abc&token=xyz")
    http_server._log_use("GET", "/s3cr3t/index.html")  # static asset: not usage

    rows = _events(log)
    assert [r["route"] for r in rows if r["method"] == "POST"] == ["/api/lead/say"] * 3
    gets = [r for r in rows if r["method"] == "GET"]
    assert len(gets) == 1 and gets[0]["route"] == "/api/lead/history"
    text = log.read_text(encoding="utf-8")
    assert "s3cr3t" not in text and "xyz" not in text and "abc" not in text
    assert all(r["event"] == "remote_use" for r in rows)


def test_breadcrumb_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENTS_LOG", tmp_path / "missing-dir" / "events.log")
    http_server._log_use("POST", "/x/api/lead/say")  # unwritable → silently skipped
