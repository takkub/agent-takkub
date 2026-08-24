"""Generic per-service circuit breaker (v2-hardening D/F, `11_CIRCUIT_
BREAKER.md`): "For every optional service: timeout, failure_count, cooldown,
next_probe, last_healthy. 3 failures -> open circuit 60s -> no calls -> one
probe -> recover if healthy. Never retry a dead optional service on every
assignment."

Before this, a down optional service (design-tool client, etc.) still paid a
full network timeout on EVERY call, every assignment — this closes that gap:
after `failure_threshold` consecutive failures the breaker opens and
`allow_call()` returns `False` immediately (no transport call, no timeout)
until `cooldown_s` has elapsed, at which point exactly one half-open probe is
let through to test recovery.

Thread-safe (one `threading.Lock` per breaker): a caller like
`design_clients` is invoked off the Qt GUI thread from a worker pool, so two
calls landing on the same breaker at once — both racing the failure_threshold,
or both racing the half-open probe slot — is a real scenario, not a
theoretical one. Each breaker guards only its own state; the module-level
registry lock (`_REGISTRY_LOCK`) is separate and only ever held for the
dict lookup/insert, never while a breaker's own lock is held, so the two
never nest and can't deadlock.

State is in-memory first (the only thing that matters for the process that
is actually placing calls) with a best-effort JSON snapshot written to
`DATA_HOME/resilience/state.json` on every state-changing transition, purely
so a SEPARATE short-lived process — `takkub doctor`, a plain CLI invocation,
never the live cockpit — can show the last-known breaker states (see
`doctor_section.py`). The snapshot is never read back into a running
breaker: each cockpit process starts every breaker CLOSED, the same way
`trace_store`'s last-context-trace file is diagnostic-only, never replayed
into a live `RetrievalEngine`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_S = 60.0


class CircuitState:
    """String states, not an `Enum` — the persisted JSON snapshot round-trips
    these as plain strings anyway (`doctor_section.py` reads them back from
    disk, in a separate process, with no `CircuitBreaker` import needed), so
    a plain namespace of string constants avoids an enum<->str conversion at
    the one boundary (persistence) that actually needs one."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitBreakerSnapshot:
    """Point-in-time view of one breaker — what `doctor`'s `[resilience]`
    section and the persisted `state.json` file both render."""

    name: str
    state: str
    failure_count: int
    failure_threshold: int
    open_until: float | None
    last_healthy: float | None
    next_probe: float | None


class CircuitBreaker:
    """One breaker for one named service.

    Contract (matches `11_CIRCUIT_BREAKER.md` exactly):
      * CLOSED — `allow_call()` always `True`. `record_failure()` increments
        a counter; at `failure_threshold` the breaker flips OPEN and stamps
        `open_until = now + cooldown_s`. `record_success()` resets the
        counter to 0 and stamps `last_healthy`.
      * OPEN — `allow_call()` returns `False` (no call attempted at all)
        until `now >= open_until`, at which point the NEXT `allow_call()`
        flips the breaker to HALF_OPEN and returns `True` — that one call is
        the probe.
      * HALF_OPEN — exactly one probe in flight at a time (a second
        `allow_call()` while one is outstanding returns `False`, so two
        threads racing the cooldown boundary can't both probe). Success ->
        CLOSED (counter reset, `last_healthy` stamped). Failure -> OPEN again
        with a fresh `cooldown_s` window — a failed probe never falls back to
        counting failures from zero, it reopens immediately.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._open_until: float | None = None
        self._last_healthy: float | None = None
        self._probe_in_flight = False

    def allow_call(self) -> bool:
        """Whether a caller may attempt a real call right now. Every `True`
        MUST be followed by exactly one of `record_success()`/
        `record_failure()` — an OPEN breaker only ever lets one HALF_OPEN
        probe through at a time, so a call that goes unrecorded (a caller
        forgetting to report the outcome) would strand the breaker OPEN
        forever, never getting another probe."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._open_until is not None and self._clock() >= self._open_until:
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    return True
                return False
            # HALF_OPEN: only one probe at a time.
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._open_until = None
            self._probe_in_flight = False
            self._last_healthy = self._clock()
        _persist_registry_state()

    def record_failure(self) -> None:
        with self._lock:
            was_half_open = self._state == CircuitState.HALF_OPEN
            self._probe_in_flight = False
            if was_half_open:
                # A failed probe reopens immediately with a fresh cooldown —
                # it does not fall back to counting failures from zero.
                self._state = CircuitState.OPEN
                self._open_until = self._clock() + self.cooldown_s
            else:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._open_until = self._clock() + self.cooldown_s
        _persist_registry_state()

    def snapshot(self) -> CircuitBreakerSnapshot:
        with self._lock:
            next_probe = self._open_until if self._state == CircuitState.OPEN else None
            return CircuitBreakerSnapshot(
                name=self.name,
                state=self._state,
                failure_count=self._failure_count,
                failure_threshold=self.failure_threshold,
                open_until=self._open_until,
                last_healthy=self._last_healthy,
                next_probe=next_probe,
            )


# ---------------------------------------------------------------------------
# process-wide registry — one breaker per service name
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_breaker(
    name: str,
    *,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
) -> CircuitBreaker:
    """The shared breaker for *name*, creating it on first use. Threshold/
    cooldown are only applied at creation time — later calls for an
    already-registered name reuse the existing breaker as-is (a second,
    different config for the same name would silently disagree with
    whichever caller registered it first, so the first caller wins rather
    than one caller's config overwriting another's mid-flight state)."""
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(name)
        if existing is not None:
            return existing
        breaker = CircuitBreaker(name, failure_threshold=failure_threshold, cooldown_s=cooldown_s)
        _REGISTRY[name] = breaker
        return breaker


def all_breakers() -> tuple[CircuitBreaker, ...]:
    with _REGISTRY_LOCK:
        return tuple(_REGISTRY.values())


def reset_registry() -> None:
    """Test-only: drop every registered breaker so the next `get_breaker`
    starts fresh. Production code never calls this — breakers are meant to
    persist for the life of the process."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


# ---------------------------------------------------------------------------
# best-effort cross-process persistence (doctor visibility only)
# ---------------------------------------------------------------------------


def _state_path():
    # Lazy import, same reason `trace_store._trace_path` gives: reading
    # `config.DATA_HOME` at call time (not at module import time) means a
    # test's `monkeypatch.setattr(config, "DATA_HOME", tmp_path)` is honoured
    # no matter when it runs relative to this module's own import.
    from agent_takkub import config

    return config.DATA_HOME / "resilience" / "state.json"


def _persist_registry_state() -> None:
    """Best-effort — a failed write never raises into `record_success`/
    `record_failure`'s caller (the same `save_last_trace` contract:
    diagnostics must never turn a successful or failed call into a crash)."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {b.name: asdict(b.snapshot()) for b in all_breakers()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("circuit_breaker: state persist failed (best-effort)", exc_info=True)


def load_persisted_state() -> dict[str, dict] | None:
    """Last-known snapshot of every breaker that has recorded at least one
    success/failure in ANY prior process — read-only, diagnostic use
    (`doctor_section.py`). `None` if the file is absent/unreadable/malformed;
    never raises."""
    try:
        path = _state_path()
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


__all__ = [
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_FAILURE_THRESHOLD",
    "CircuitBreaker",
    "CircuitBreakerSnapshot",
    "CircuitState",
    "all_breakers",
    "get_breaker",
    "load_persisted_state",
    "reset_registry",
]
