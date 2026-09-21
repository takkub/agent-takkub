"""Minimal stdlib client for TypeSafe AI's System One endpoint (the Jev model).

`POST https://api.typesafe.ai/v1/systemone` with `Authorization: Bearer <key>`
takes ``{state, model, questions}`` and answers with a typed value plus a
calibrated probability/confidence per question. That is the whole surface the
cockpit uses, so this is hand-rolled on `urllib` instead of depending on the
vendor's `typesafe-sdk`:

* the cockpit ships to users over npm and the decision layer on top of this
  (`decide.py`) is **off by default** — an install that never enables it must
  not grow a runtime dependency, nor an import that can fail;
* import-linter: this module imports nothing from `agent_takkub` at all, so it
  is a pure stdlib leaf importable from any layer (same shape as `task_scope`).

**Never raises.** Every failure — no key, HTTP 4xx/5xx, timeout, DNS, body that
does not match the documented schema — comes back as ``None`` from :func:`ask`
with the reason on :func:`last_error`, because every caller already has a local
fallback (the regex classifier). A cloud decision service must never be able to
take the cockpit down.

**Costs money.** :func:`ask` is metered (:func:`stats`) and capped per process
(:data:`MAX_CALLS`, env ``TAKKUB_TYPESAFE_MAX_CALLS``) so a runaway caller
burns a bounded amount before it starts refusing. The cap is per process, not
per day. The Settings Usage page (`settings_usage._build_decide_panel`) shows
this meter and holds the stored key `api_key()` falls back to.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

# Measured from Bangkok 2026-09-21 against jev-1.13.0: p50 793 ms, max 1404 ms
# over 8 calls (the vendor's "40-200x faster than a frontier LLM" is compute,
# not round-trip). 6 s therefore allows ~4x the observed worst case while still
# being a cap a human notices rather than waits out.
DEFAULT_TIMEOUT = 6.0

# `TAKKUB_*` first so the cockpit can be pointed at its own key without
# disturbing a `TYPESAFE_API_KEY` the user set for their own projects; the
# vendor's own variable is honoured second so an existing setup just works.
ENV_KEYS: tuple[str, ...] = ("TAKKUB_TYPESAFE_API_KEY", "TYPESAFE_API_KEY")

MAX_CALLS = 500


@dataclass(frozen=True)
class Answer:
    """One question's typed answer.

    `value` is the choice id (choice), the score as a float (score), or the
    probability of yes (noul). `confidence` is ``None`` for a noul — the API
    does not report one for that primitive — so callers must treat missing
    confidence as "unknown", never as 0.0 (which would read as no confidence).
    """

    kind: str
    value: Any
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Reply:
    model: str
    answers: dict[str, Answer]
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: float = 0.0


_lock = threading.Lock()
_thread_state = threading.local()
_calls = 0
_failures = 0
_in_tokens = 0
_out_tokens = 0
_total_ms = 0.0
_last_error = ""


# Fallback source for the key when no env var is set — the Settings page's
# stored key, registered by `decide` as a callable so this module stays a pure
# stdlib leaf (importing the settings store here would invert the layering).
_key_loader: Callable[[], str] | None = None


def set_key_loader(loader: Callable[[], str] | None) -> None:
    global _key_loader
    _key_loader = loader


def api_key() -> str:
    for name in ENV_KEYS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if _key_loader is not None:
        try:
            return (_key_loader() or "").strip()
        except Exception:
            return ""
    return ""


def available() -> bool:
    """True when a key is configured. Says nothing about reachability."""
    return bool(api_key())


def last_error() -> str:
    """Why the CALLING THREAD's most recent :func:`ask` failed, falling back to
    the process-wide one when this thread has not failed yet.

    Per thread on purpose: shadow mode probes on four `bg_pool` workers at once,
    and a single global would let one worker's row be logged with another's
    error — the comparison log would then blame the wrong task.
    """
    mine = getattr(_thread_state, "last_error", None)
    if mine is not None:
        # "" is a real answer here (this thread's last call SUCCEEDED) and must
        # not fall through to another thread's error — only "never called on
        # this thread" (None) defers to the process-wide value.
        return mine
    with _lock:
        return _last_error


def stats() -> dict[str, Any]:
    """Counters for the cost meter — calls made, failures, tokens, latency."""
    with _lock:
        return {
            "calls": _calls,
            "failures": _failures,
            "input_tokens": _in_tokens,
            "output_tokens": _out_tokens,
            "total_ms": round(_total_ms, 1),
            # the live budget, not the constant — an operator who set
            # TAKKUB_TYPESAFE_MAX_CALLS must see the cap the meter is measured
            # against, or the UI would show a limit that is not the real one.
            "max_calls": _max_calls(),
            "last_error": _last_error,
        }


def reset_stats() -> None:
    """Test-only: clears the meter and the per-process call budget."""
    global _calls, _failures, _in_tokens, _out_tokens, _total_ms, _last_error
    with _lock:
        _calls = _failures = _in_tokens = _out_tokens = 0
        _total_ms = 0.0
        _last_error = ""
    _thread_state.last_error = None  # back to "never called on this thread"


def _max_calls() -> int:
    raw = (os.environ.get("TAKKUB_TYPESAFE_MAX_CALLS") or "").strip()
    if raw.isdigit():
        return int(raw)
    return MAX_CALLS


def _note_failure(msg: str) -> None:
    global _failures, _last_error
    _thread_state.last_error = msg
    with _lock:
        _failures += 1
        _last_error = msg


def _as_int(value: Any) -> int:
    """Token counts as reported, accepting the float form too — `392.0` must not
    meter as 0 and silently understate the bill."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _parse_answer(raw: Any) -> Answer | None:
    """One answer object -> :class:`Answer`, or ``None`` if it is not shaped
    like any documented primitive.

    Upstream schema drift must surface as a fallback to the local classifier,
    never as an exception or a silently wrong tier — so an unknown `type`, or a
    known type missing its value field, is rejected here rather than coerced.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    probs_raw = raw.get("probabilities")
    probs: dict[str, float] = {}
    if isinstance(probs_raw, dict):
        for key, val in probs_raw.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                probs[str(key)] = float(val)
    conf = raw.get("confidence")
    confidence = (
        float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    )

    if kind == "choice":
        value = raw.get("choice")
        if not isinstance(value, str) or not value:
            return None
        return Answer("choice", value, confidence, probs)
    if kind == "score":
        value = raw.get("score")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return Answer("score", float(value), confidence, probs)
    if kind == "noul":
        value = raw.get("noul")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return Answer("noul", float(value), confidence, probs)
    return None


def ask(
    state: str,
    questions: dict[str, dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
) -> Reply | None:
    """Ask every question in `questions` about `state` in ONE call.

    Batching matters: the vendor measures a 13-question briefing as 12.2x
    cheaper and 10.0x faster batched than as separate calls, so callers should
    add speculative questions here rather than make a second request.

    Returns ``None`` on any failure (see module docstring).
    """
    global _calls, _in_tokens, _out_tokens, _total_ms

    if not questions:
        _note_failure("no questions")
        return None
    key = api_key()
    if not key:
        _note_failure("no API key (TAKKUB_TYPESAFE_API_KEY)")
        return None

    cap = _max_calls()
    with _lock:
        if _calls >= cap:
            spent = _calls
        else:
            spent = -1
            _calls += 1
    if spent >= 0:
        _note_failure(f"per-process call cap reached ({spent}/{cap})")
        return None

    body = json.dumps(
        {"state": state, "model": model, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        _note_failure(f"HTTP {exc.code} {detail}".strip())
        return None
    except Exception as exc:
        _note_failure(f"{type(exc).__name__}: {exc}")
        return None
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        _note_failure("response has no answers object")
        return None
    answers: dict[str, Answer] = {}
    for name, raw in payload["answers"].items():
        parsed = _parse_answer(raw)
        if parsed is not None:
            answers[str(name)] = parsed
    if not answers:
        _note_failure("no answer matched a known primitive (schema drift?)")
        return None

    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tok_in = _as_int(usage.get("input_tokens"))
    tok_out = _as_int(usage.get("output_tokens"))
    with _lock:
        _in_tokens += tok_in
        _out_tokens += tok_out
        _total_ms += elapsed_ms

    _thread_state.last_error = ""  # a success must not leave a stale reason behind
    return Reply(
        model=str(payload.get("model") or model),
        answers=answers,
        input_tokens=tok_in,
        output_tokens=tok_out,
        elapsed_ms=elapsed_ms,
    )
