"""Unit tests for cockpit crash auto-capture (src/agent_takkub/auto_issue_capture.py)."""

from __future__ import annotations

import json
import sys

import pytest

from agent_takkub import auto_issue_capture as aic


class _SyncThread:
    """Runs target() inline instead of on a real thread, so tests are
    deterministic and never race the background worker."""

    def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


def _make_exc(cls: type[BaseException], msg: str = "boom"):
    """Raise-and-catch to get a real (exc_type, exc_value, exc_tb) triple."""
    try:
        raise cls(msg)
    except cls:
        return sys.exc_info()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(aic, "_DEDUP_PATH", tmp_path / "auto_issue_dedup.json")
    monkeypatch.setattr(aic, "_spawn", _SyncThread)
    monkeypatch.setattr(aic, "_recent", {})
    monkeypatch.setattr(aic, "_fired_mem", [])
    monkeypatch.setattr(aic, "_signatures_mem", {})
    monkeypatch.setattr(aic, "_cap_blocked_until", 0.0)
    monkeypatch.setattr(aic, "_persist_broken", False)


def test_same_signature_within_cooldown_does_not_refire(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(ValueError, "boom once")
    aic.capture_cockpit_crash(et, ev, tb, source="test")
    et, ev, tb = _make_exc(ValueError, "boom twice")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 1


def test_different_signatures_both_fire(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(ValueError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")
    et, ev, tb = _make_exc(TypeError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 2


def test_rate_cap_blocks_sixth_distinct_signature(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    exc_types = [ValueError, TypeError, KeyError, IndexError, AttributeError, RuntimeError]
    for cls in exc_types:
        et, ev, tb = _make_exc(cls, "boom")
        aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 5, "6th distinct signature must not exceed the 5/24h rate cap"


def test_rate_cap_holds_exact_count_when_clock_does_not_advance(monkeypatch):
    """R2-M1 regression: Windows clock resolution is 15.6ms, so several
    reservations can land on the same time.time() value. The cap must still
    count them as distinct entries instead of set-deduping them away."""
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )
    monkeypatch.setattr(aic.time, "time", lambda: 1_000_000.0)

    exc_types = [
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        RuntimeError,
        OSError,
        LookupError,
        ArithmeticError,
        NameError,
    ]
    for cls in exc_types:
        et, ev, tb = _make_exc(cls, "boom")
        aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 5, "rate cap must hold exactly at 5 even when time.time() never advances"


def test_cap_rejected_signature_can_refire_once_a_slot_frees_up(monkeypatch):
    """R2-L2 regression: a signature rejected by the *rate cap* (never
    actually filed) must not stay blocked by the in-process _recent
    short-circuit until its own 24h cooldown elapses — it should refire as
    soon as the cap frees a slot."""
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )
    fake_now = [0.0]
    monkeypatch.setattr(aic.time, "time", lambda: fake_now[0])

    # Fill the cap: 5 distinct signatures fired at t=0.
    exc_types = [ValueError, TypeError, KeyError, IndexError, AttributeError]
    for cls in exc_types:
        et, ev, tb = _make_exc(cls, "boom")
        aic.capture_cockpit_crash(et, ev, tb, source="test")
    assert len(calls) == 5

    # A 6th, distinct signature attempted well within the same 24h window:
    # rejected by the rate cap, not filed.
    fake_now[0] = 20 * 60 * 60
    et, ev, tb = _make_exc(RuntimeError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")
    assert len(calls) == 5

    # The oldest of the 5 ages out of the rolling window, freeing a slot —
    # but only 4h have passed since the rejected attempt, well under its own
    # 24h cooldown.
    fake_now[0] = 24 * 60 * 60 + 1
    et, ev, tb = _make_exc(RuntimeError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")
    assert len(calls) == 6, (
        "a cap-rejected signature must refire once a slot frees, not wait out its own cooldown"
    )


def test_new_issue_failure_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("gh not authenticated")

    monkeypatch.setattr(aic.issues, "new_issue", _boom)

    et, ev, tb = _make_exc(ValueError, "boom")
    # Must not raise — a failure filing the auto-issue must never escalate
    # into a second unhandled exception inside the exception hook itself.
    aic.capture_cockpit_crash(et, ev, tb, source="test")


def test_new_issue_failure_still_consumes_the_slot(monkeypatch):
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh down"))
    )

    et, ev, tb = _make_exc(ValueError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    state = json.loads(aic._DEDUP_PATH.read_text(encoding="utf-8"))
    assert len(state["fired"]) == 1


def test_cockpit_bug_flag_is_passed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(ValueError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert calls[0][1]["cockpit_bug"] is True
    assert calls[0][1]["noticed_in"] == "cockpit"


def test_rolling_window_lets_signature_refire_after_24h(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    fake_now = [1_000_000.0]
    monkeypatch.setattr(aic.time, "time", lambda: fake_now[0])

    et, ev, tb = _make_exc(ValueError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    fake_now[0] += 25 * 60 * 60  # advance 25h — past both the 24h cooldown and rate window
    et, ev, tb = _make_exc(ValueError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 2, "same signature must be able to refire once the 24h window rolls past"


def test_corrupt_state_file_does_not_permanently_disable_capture(monkeypatch):
    aic._DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    aic._DEDUP_PATH.write_text(
        json.dumps(
            {"fired": ["not-a-number", None], "signatures": {"ValueError:x.py:1": "also-bad"}}
        ),
        encoding="utf-8",
    )

    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(ValueError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 1


def test_oversize_traceback_and_message_are_capped(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(ValueError, "x" * 50_000)
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    _title, body = calls[0][0]
    assert len(body) < 50_000
    assert "x" * 50_000 not in body


def test_exception_message_with_token_is_redacted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    secret = "ghp_" + "A" * 36
    et, ev, tb = _make_exc(RuntimeError, f"Command failed with token {secret} in argv")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    title, body = calls[0][0]
    assert secret not in title
    assert secret not in body
    assert "[redacted]" in body


def test_add_authtoken_argv_form_is_redacted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    secret = "2abcSECRETtoken"
    msg = f"Command '['ngrok', 'add-authtoken', '{secret}']' timed out after 15 seconds"
    et, ev, tb = _make_exc(RuntimeError, msg)
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    title, body = calls[0][0]
    assert secret not in title
    assert secret not in body


@pytest.mark.parametrize(
    "secret,msg",
    [
        ("abc123XYZshort", "Command '['gh', '--token', 'abc123XYZshort']' failed"),
        ("shortKEY1234", "Command '['x', '--api-key', 'shortKEY1234']' failed"),
        ("hunter2pass", "Command '['psql', '--password=hunter2pass']' failed"),
        ("abc123short", "curl -H 'Authorization: Bearer abc123short'"),
        ("myShortKey123", "ANTHROPIC_AUTH_TOKEN=myShortKey123"),
        ("abcd1234efgh", '{"token": "abcd1234efgh"}'),
        ("s3cretPw", "Failed cloning https://user:s3cretPw@host/repo.git"),
    ],
)
def test_redact_covers_round2_leak_shapes(monkeypatch, secret, msg):
    """R2-M2 regression: these all leaked past the round-1 redaction patterns
    (argv-repr quote/comma separators, short secrets below the 32-char
    catch-all floor, and basic-auth URL creds with no keyword nearby)."""
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(RuntimeError, msg)
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    title, body = calls[0][0]
    assert secret not in title
    assert secret not in body


def test_scrub_home_removes_home_directory():
    home_str = str(aic.Path.home())
    if not home_str:
        pytest.skip("no home directory available in this environment")

    text = f'File "{home_str}\\project\\app.py", line 10, in run'
    scrubbed = aic._scrub_home(text)

    assert home_str not in scrubbed
    assert "~" in scrubbed


def test_scrub_home_removes_lowercase_home_path():
    """R2-M3 regression: str.replace is exact-match, so a lowercased home
    path (e.g. gemini_helper.py's .as_posix().lower()) used to survive."""
    home_str = str(aic.Path.home())
    if not home_str:
        pytest.skip("no home directory available in this environment")

    text = f'File "{home_str.lower()}\\project\\app.py", line 10, in run'
    scrubbed = aic._scrub_home(text)

    assert home_str.lower() not in scrubbed
    assert "~" in scrubbed


def test_scrub_home_removes_forward_slash_home_path():
    """R2-M3 regression: 19 call sites in this repo use .as_posix(), which
    turns the home path into forward slashes that used to survive the scrub."""
    home_str = str(aic.Path.home())
    if not home_str:
        pytest.skip("no home directory available in this environment")

    posix_home = home_str.replace("\\", "/")
    text = f'File "{posix_home}/project/app.py", line 10, in run'
    scrubbed = aic._scrub_home(text)

    assert posix_home not in scrubbed
    assert "~" in scrubbed


def test_title_never_contains_raw_exception_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )

    et, ev, tb = _make_exc(ValueError, "some secret detail that must stay out of the title")
    aic.capture_cockpit_crash(et, ev, tb, source="test")

    title, _body = calls[0][0]
    assert "secret detail" not in title
    assert title.startswith("[auto] ValueError @ ValueError")


def test_rate_cap_blocked_until_prevents_thread_storm_after_cap_full(monkeypatch):
    """R3-M1 regression: once the 5/5 rate cap is reached, subsequent crash
    reports within the block period must be short-circuited inside the lock
    before spawning a background worker thread."""
    calls = []
    monkeypatch.setattr(
        aic.issues, "new_issue", lambda *a, **k: calls.append((a, k)) or (1, "local://issue/1")
    )
    spawn_calls = 0

    class _TrackingSyncThread(_SyncThread):
        def __init__(self, *args, **kwargs) -> None:
            nonlocal spawn_calls
            spawn_calls += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(aic, "_spawn", _TrackingSyncThread)

    fake_now = [1_000_000.0]
    monkeypatch.setattr(aic.time, "time", lambda: fake_now[0])

    # Fill the cap: 5 distinct signatures at t=1,000,000.
    exc_types = [ValueError, TypeError, KeyError, IndexError, AttributeError]
    for cls in exc_types:
        et, ev, tb = _make_exc(cls, "boom")
        aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 5
    assert spawn_calls == 5

    # 6th attempt triggers rate cap block, setting _cap_blocked_until.
    et, ev, tb = _make_exc(RuntimeError, "boom")
    aic.capture_cockpit_crash(et, ev, tb, source="test")
    assert len(calls) == 5
    assert spawn_calls == 6

    # Subsequent 100 crashes during the block window must NOT spawn any more threads.
    fake_now[0] += 100  # well within the 24h block window
    for _ in range(100):
        et, ev, tb = _make_exc(RuntimeError, "boom storm")
        aic.capture_cockpit_crash(et, ev, tb, source="test")

    assert len(calls) == 5
    assert spawn_calls == 6, (
        "crash storm after cap hit must be blocked before spawning background threads"
    )
