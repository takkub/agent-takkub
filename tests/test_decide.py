"""`decide` (scope-decision seam) + `typesafe_bridge` (System One HTTP client).

Everything here is offline: the bridge is exercised by patching `urlopen` with
the response body copied verbatim from the vendor's documented schema, and the
seam is exercised by patching `typesafe_bridge.ask`. No test may reach the
network — the "off" tests assert that explicitly, because a suite that silently
started calling a paid API would be discovered by the bill.

The response fixtures are the upstream-schema-drift guard: if TypeSafe renames
`choice`/`confidence`/`probabilities`, `test_schema_drift_*` fails here rather
than the cockpit silently mis-sizing every task at the vendor's next deploy.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from agent_takkub import config, core_v2_settings, decide, task_scope, typesafe_bridge

# Copied verbatim from https://docs.typesafe.ai/introduction/quickstart (2026-09-21),
# trimmed to the two questions `decide` actually asks.
DOC_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "scope": {
            "type": "choice",
            "choice": "deep",
            "confidence": 0.78,
            "probabilities": {"deep": 0.85, "normal": 0.15, "tiny": 0.0},
        },
        "read_only": {"type": "noul", "noul": 0.03},
    },
    "usage": {"input_tokens": 392, "output_tokens": 65},
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    for name in ("TAKKUB_DECIDE_MODE", *typesafe_bridge.ENV_KEYS, "TAKKUB_TYPESAFE_MAX_CALLS"):
        monkeypatch.delenv(name, raising=False)
    core_v2_settings._reset_cache()
    typesafe_bridge.reset_stats()
    # The suite holds a session-wide QApplication, so the real guard reports
    # "on the GUI thread" for every test here and "on" mode would always degrade
    # to shadow. Default it to off-thread and let the one Qt test opt back in.
    monkeypatch.setattr(decide, "_on_qt_main_thread", lambda: False)
    yield
    core_v2_settings._reset_cache()
    typesafe_bridge.reset_stats()


def _reply(tier: str, confidence: float | None, *, read_only: float = 0.0):
    answers = {
        "scope": typesafe_bridge.Answer("choice", tier, confidence, {tier: 1.0}),
        "read_only": typesafe_bridge.Answer("noul", read_only, None, {}),
    }
    return typesafe_bridge.Reply("jev-1.13.0", answers, 392, 65, 812.0)


def _events(monkeypatch=None) -> list[dict]:
    path = config.EVENTS_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _explode(*_a, **_kw):
    raise AssertionError("the network must not be touched in this mode")


# ── persisted knob ────────────────────────────────────────────────────────────


def test_default_mode_is_off():
    assert core_v2_settings.load_decide_mode() == "off"
    assert decide.mode() == "off"


@pytest.mark.parametrize("value", ["off", "shadow", "on"])
def test_mode_round_trips(value):
    assert core_v2_settings.save_decide_mode(value) is True
    assert core_v2_settings.load_decide_mode() == value
    assert decide.mode() == value


def test_saving_unknown_mode_raises():
    with pytest.raises(ValueError):
        core_v2_settings.save_decide_mode("ludicrous")


def test_corrupt_on_disk_mode_reads_as_off():
    path = core_v2_settings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"decide_mode": "turbo"}), encoding="utf-8")
    core_v2_settings._reset_cache()
    assert core_v2_settings.load_decide_mode() == "off"
    assert decide.mode() == "off"


def test_older_settings_file_without_the_key_reads_as_off():
    path = core_v2_settings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"context_strategy": "deep"}), encoding="utf-8")
    core_v2_settings._reset_cache()
    assert core_v2_settings.load_decide_mode() == "off"


def test_saving_mode_does_not_clobber_context_strategy():
    core_v2_settings.save_context_strategy("deep")
    core_v2_settings.save_decide_mode("shadow")
    assert core_v2_settings.load_context_strategy() == "deep"
    assert core_v2_settings.load_decide_mode() == "shadow"


def test_mode_tuples_stay_in_sync():
    """The settings store spells the modes literally to avoid an import cycle."""
    assert core_v2_settings._DECIDE_MODES == decide.MODES
    assert core_v2_settings._DEFAULT_DECIDE_MODE == decide.DEFAULT_MODE


def test_env_wins_over_persisted_mode(monkeypatch):
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_DECIDE_MODE", "off")
    assert decide.mode() == "off"


def test_invalid_env_falls_back_to_persisted_mode(monkeypatch):
    core_v2_settings.save_decide_mode("shadow")
    monkeypatch.setenv("TAKKUB_DECIDE_MODE", "ludicrous")
    assert decide.mode() == "shadow"


# ── mode off: the cockpit must behave exactly as before ───────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "แก้ typo ในหน้า Settings",
        "ย้าย storage V1 → V2 ทั้งระบบ ห้ามข้อมูลหาย",
        "หมุน api key ของ provider ทุกตัว",
        "",
    ],
)
def test_off_mode_is_exactly_task_scope(text, monkeypatch):
    monkeypatch.setattr(typesafe_bridge, "ask", _explode)
    assert decide.scope(text) == task_scope.classify(text)


def test_empty_task_never_calls_the_model_even_when_on(monkeypatch):
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setattr(typesafe_bridge, "ask", _explode)
    assert decide.scope("   ").scope == task_scope.classify("   ").scope


# ── mode on ───────────────────────────────────────────────────────────────────


def test_on_without_a_key_uses_the_regex_tables_and_says_so(monkeypatch):
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setattr(typesafe_bridge, "ask", _explode)  # never reached: no key
    got = decide.scope("ย้าย storage V1 → V2 ทั้งระบบ")
    assert got.scope == task_scope.classify("ย้าย storage V1 → V2 ทั้งระบบ").scope
    assert "API key" in got.reason


def test_confident_model_answer_overrides_the_regex_tables(monkeypatch):
    """The live 2026-09-21 miss: the table sizes this `normal` because it knows
    `migrate` but not the ordinary Thai word "ย้าย"."""
    text = "ย้าย storage V1 → V2 ทั้งระบบ ห้ามข้อมูลหายแม้แต่ไฟล์เดียว"
    assert task_scope.classify(text).scope == "normal"

    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("deep", 1.0))

    got = decide.scope(text)
    assert got.scope == "deep"
    assert "jev" in got.reason
    logged = [e for e in _events() if e["event"] == "decide_scope"]
    assert logged and logged[-1]["source"] == "jev"
    assert logged[-1]["local"] == "normal"


def test_low_confidence_takes_the_more_cautious_of_the_two(monkeypatch):
    """Under-sizing skips qa/reviewer (#585), so ambiguity rounds upward."""
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("tiny", 0.41))

    text = "หมุน api key ของ provider ทุกตัวแล้วอัปเดต CI secret"
    assert task_scope.classify(text).scope == "deep"
    got = decide.scope(text)
    assert got.scope == "deep"  # not the model's "tiny"
    assert "ไม่มั่นใจ" in got.reason
    assert [e for e in _events() if e["event"] == "decide_scope"][-1]["source"] == "floor"


def test_low_confidence_still_wins_when_the_model_is_the_cautious_one(monkeypatch):
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("deep", 0.5))
    text = "แก้ typo ในหน้า Settings"
    assert task_scope.classify(text).scope == "tiny"
    assert decide.scope(text).scope == "deep"


def test_api_failure_falls_back_to_the_regex_tables(monkeypatch):
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: None)
    monkeypatch.setattr(typesafe_bridge, "last_error", lambda: "TimeoutError: timed out")

    text = "แก้ typo ในหน้า Settings"
    got = decide.scope(text)
    assert got.scope == task_scope.classify(text).scope
    assert "jev ไม่ตอบ" in got.reason


def test_tier_outside_the_known_set_is_rejected(monkeypatch):
    """A vendor-side rename of an option id must not become a bogus tier."""
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("enormous", 1.0))
    monkeypatch.setattr(typesafe_bridge, "last_error", lambda: "")
    assert decide.scope("แก้ typo").scope == "tiny"


# ── shadow mode & the Qt-main-thread guard ────────────────────────────────────


def test_shadow_mode_returns_the_local_answer_and_logs_both(monkeypatch):
    core_v2_settings.save_decide_mode("shadow")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("deep", 0.99))
    # run the probe inline instead of on the pool so the assertion is not a race
    from agent_takkub import bg_pool

    monkeypatch.setattr(bg_pool, "submit", lambda fn, *a, **k: fn(*a, **k))

    text = "ย้าย storage V1 → V2 ทั้งระบบ"
    got = decide.scope(text)
    assert got == task_scope.classify(text)  # behaviour unchanged

    rows = [e for e in _events() if e["event"] == "decide_shadow"]
    assert len(rows) == 1
    assert rows[0]["local"] == "normal"
    assert rows[0]["jev"] == "deep"
    assert rows[0]["agree"] is False


def test_shadow_mode_logs_the_error_when_the_model_is_unreachable(monkeypatch):
    core_v2_settings.save_decide_mode("shadow")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: None)
    monkeypatch.setattr(typesafe_bridge, "last_error", lambda: "HTTP 401")
    from agent_takkub import bg_pool

    monkeypatch.setattr(bg_pool, "submit", lambda fn, *a, **k: fn(*a, **k))

    decide.scope("แก้ typo ในหน้า Settings")
    rows = [e for e in _events() if e["event"] == "decide_shadow"]
    assert rows and rows[0]["error"] == "HTTP 401"


def test_on_the_qt_main_thread_on_mode_never_blocks(monkeypatch):
    """A 800 ms round trip on the GUI thread is a visible freeze — "on" degrades
    to shadow behaviour there (regex answers, model probed in background)."""
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(decide, "_on_qt_main_thread", lambda: True)
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("deep", 1.0))
    submitted: list = []
    from agent_takkub import bg_pool

    monkeypatch.setattr(bg_pool, "submit", lambda fn, *a, **k: submitted.append(fn))

    text = "ย้าย storage V1 → V2 ทั้งระบบ"
    got = decide.scope(text)
    assert got.scope == task_scope.classify(text).scope
    assert submitted, "the model should still be probed off-thread"
    rows = [e for e in _events() if e["event"] == "decide_scope"]
    assert rows and rows[-1]["reason"] == "qt-main"


def test_shadow_log_survives_concurrent_writers():
    """Shadow mode logs from `bg_pool`'s 4 workers at once. Python's append mode
    on Windows is seek-then-write, not atomic: before `_log_lock`, 8 threads x
    200 rows landed 1483/1600 (measured 2026-09-21) — silently dropped rows that
    would have understated the disagreement rate the comparison exists to
    measure."""
    import threading

    workers, per_worker = 8, 25

    def hammer(which: int) -> None:
        for i in range(per_worker):
            decide._log("decide_shadow", worker=which, i=i, pad="x" * 120)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = _events()
    assert len(rows) == workers * per_worker
    assert {(r["worker"], r["i"]) for r in rows} == {
        (w, i) for w in range(workers) for i in range(per_worker)
    }


def test_qt_main_thread_probe_never_raises(monkeypatch):
    monkeypatch.undo()  # restore the real implementation the fixture stubbed out
    assert decide._on_qt_main_thread() in (True, False)


# ── typesafe_bridge ───────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _patch_urlopen(monkeypatch, payload, captured=None):
    def fake(req, timeout=None):
        if captured is not None:
            captured.append((req, timeout))
        return _FakeResponse(payload)

    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", fake)


def test_ask_parses_the_documented_response(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    captured: list = []
    _patch_urlopen(monkeypatch, DOC_RESPONSE, captured)

    reply = typesafe_bridge.ask("some task", {"scope": decide.SCOPE_QUESTION})
    assert reply is not None
    assert reply.model == "jev-1.13.0"
    assert reply.answers["scope"].value == "deep"
    assert reply.answers["scope"].confidence == pytest.approx(0.78)
    assert reply.answers["scope"].probabilities["deep"] == pytest.approx(0.85)
    # a noul carries no confidence — None, never 0.0 (which reads as "no confidence")
    assert reply.answers["read_only"].value == pytest.approx(0.03)
    assert reply.answers["read_only"].confidence is None
    assert (reply.input_tokens, reply.output_tokens) == (392, 65)

    req, timeout = captured[0]
    assert req.full_url == typesafe_bridge.ENDPOINT
    assert req.get_header("Authorization") == "Bearer test-key"
    assert timeout == typesafe_bridge.DEFAULT_TIMEOUT
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == typesafe_bridge.DEFAULT_MODEL
    assert body["state"] == "some task"

    stats = typesafe_bridge.stats()
    assert stats["calls"] == 1 and stats["failures"] == 0
    assert stats["input_tokens"] == 392


def test_both_questions_ride_in_one_request(monkeypatch):
    """Batching is 12.2x cheaper than separate calls (vendor's own measurement),
    so the scope question and its explanatory noul must not be two round trips."""
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    captured: list = []
    _patch_urlopen(monkeypatch, DOC_RESPONSE, captured)

    decide._ask_jev("ย้าย storage V1 → V2")
    assert len(captured) == 1
    body = json.loads(captured[0][0].data.decode("utf-8"))
    assert set(body["questions"]) == {"scope", "read_only"}


def test_ask_without_a_key_returns_none_without_calling_out(monkeypatch):
    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", _explode)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None
    assert "API key" in typesafe_bridge.last_error()


def test_ask_with_no_questions_is_refused(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", _explode)
    assert typesafe_bridge.ask("x", {}) is None


def test_http_error_returns_none_and_is_metered(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(typesafe_bridge.ENDPOINT, 429, "Too Many", {}, None)

    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", boom)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None
    assert "429" in typesafe_bridge.last_error()
    assert typesafe_bridge.stats()["failures"] == 1


def test_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")

    def boom(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", boom)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None
    assert "TimeoutError" in typesafe_bridge.last_error()


def test_call_cap_stops_spending(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TAKKUB_TYPESAFE_MAX_CALLS", "1")
    _patch_urlopen(monkeypatch, DOC_RESPONSE)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is not None

    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", _explode)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None
    assert "cap reached" in typesafe_bridge.last_error()


@pytest.mark.parametrize(
    "answers",
    [
        {"scope": {"type": "selection", "selection": "deep"}},  # renamed primitive
        {"scope": {"type": "choice", "selected": "deep"}},  # renamed value field
        {"scope": {"type": "choice", "choice": ""}},  # empty option id
        {"scope": "deep"},  # answer is no longer an object
    ],
)
def test_schema_drift_is_rejected_not_guessed(monkeypatch, answers):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    _patch_urlopen(monkeypatch, {"model": "jev-9", "answers": answers})
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None
    assert typesafe_bridge.stats()["failures"] == 1


def test_schema_drift_at_the_top_level_is_rejected(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    _patch_urlopen(monkeypatch, {"model": "jev-9", "results": {}})
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None


def test_score_and_bool_edges(monkeypatch):
    """`True` is an int in Python — a bool must never pass as a score/noul."""
    assert typesafe_bridge._parse_answer({"type": "score", "score": True}) is None
    assert typesafe_bridge._parse_answer({"type": "noul", "noul": False}) is None
    parsed = typesafe_bridge._parse_answer({"type": "score", "score": 2, "confidence": 1.0})
    assert parsed is not None and parsed.value == 2.0


def test_missing_confidence_is_untrusted_not_certain(monkeypatch):
    """A choice arriving without `confidence` (a schema change) must fall to the
    safety floor, not be treated as 100% certain — otherwise the drift would
    quietly hand the model unchecked authority over the tier."""
    core_v2_settings.save_decide_mode("on")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_bridge, "ask", lambda *a, **k: _reply("tiny", None))

    text = "หมุน api key ของ provider ทุกตัวแล้วอัปเดต CI secret"
    assert task_scope.classify(text).scope == "deep"
    got = decide.scope(text)
    assert got.scope == "deep"  # the floor held
    assert [e for e in _events() if e["event"] == "decide_scope"][-1]["source"] == "floor"


def test_meter_reports_the_live_budget_not_the_constant(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_MAX_CALLS", "7")
    assert typesafe_bridge.stats()["max_calls"] == 7


def test_float_token_counts_are_metered(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    payload = dict(DOC_RESPONSE, usage={"input_tokens": 392.0, "output_tokens": 65.0})
    _patch_urlopen(monkeypatch, payload)
    reply = typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION})
    assert reply is not None
    assert (reply.input_tokens, reply.output_tokens) == (392, 65)
    assert typesafe_bridge.stats()["input_tokens"] == 392


def test_last_error_is_per_thread(monkeypatch):
    """Four `bg_pool` workers fail independently in shadow mode; a global would
    let one worker's row be logged with another worker's error."""
    import threading

    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")

    def fake(req, timeout=None):
        raise TimeoutError(threading.current_thread().name)

    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", fake)
    seen: dict[str, str] = {}

    def worker() -> None:
        typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION})
        seen[threading.current_thread().name] = typesafe_bridge.last_error()

    threads = [threading.Thread(target=worker, name=f"w{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 4
    for name, err in seen.items():
        assert err.endswith(name), f"{name} saw {err!r}"


def test_a_success_clears_this_thread_s_error(monkeypatch):
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")

    def boom(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(typesafe_bridge.urllib.request, "urlopen", boom)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is None
    assert typesafe_bridge.last_error()

    _patch_urlopen(monkeypatch, DOC_RESPONSE)
    assert typesafe_bridge.ask("x", {"scope": decide.SCOPE_QUESTION}) is not None
    assert typesafe_bridge.last_error() == ""


def test_stats_exposes_mode_and_key_presence(monkeypatch):
    core_v2_settings.save_decide_mode("shadow")
    monkeypatch.setenv("TAKKUB_TYPESAFE_API_KEY", "test-key")
    snapshot = decide.stats()
    assert snapshot["mode"] == "shadow"
    assert snapshot["key_present"] is True
    assert snapshot["max_calls"] == typesafe_bridge.MAX_CALLS
