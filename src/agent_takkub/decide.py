"""The single seam where a task's scope tier may be decided by a model instead
of by `task_scope`'s regex tables.

Why a seam and not a rewrite: `task_scope.classify()` has exactly three callers
(`cli.assign`, `orchestrator._do_assign`, `routing_planner.classify`) and a
12 KB test file pinning its behaviour. This module wraps it rather than
replacing it, so the regex path — and every test of it — stays untouched and
remains the offline fallback.

Three modes, resolved by :func:`mode` (env ``TAKKUB_DECIDE_MODE`` wins over the
persisted setting, the precedence shape every other knob here uses):

``off`` (default)
    Pure `task_scope`. No network, no key, no cost. A cockpit that never opts
    in behaves exactly as it did before this module existed.
``shadow``
    `task_scope` still decides. Jev is asked in parallel on `bg_pool` and both
    answers are logged to ``events.log`` (event ``decide_shadow``) so the two
    can be compared on real traffic before anyone trusts the model. Costs money,
    changes no behaviour.
``on``
    Jev decides, with `task_scope` as the fallback for every failure and as the
    safety floor for a low-confidence answer (see :func:`scope`).

**Why a model here at all** — measured 2026-09-21 on 8 real task texts, regex
5/8 vs jev-1.13.0 8/8. The regex misses are the shape a keyword table cannot
fix: "ย้าย storage V1 -> V2 ทั้งระบบ" sized *normal* because the table knows
`migrate`/`ไมเกรต` but not the ordinary Thai word "ย้าย" (a data migration with
no deep ceremony), while "อ่าน schema ... ห้ามแก้" sized *deep* because `schema`
outranks every read-only signal.

**Not built yet** (deliberately out of this change): the Settings UI with the
3-way switch and the spend meter, and a budget that survives a restart
(`typesafe_bridge.MAX_CALLS` is per process). Until that ships, the mode is set
by env var or by `core_v2_settings.save_decide_mode`, which is enough to run
shadow mode and read the numbers back out of `events.log`.

Import note: `config.EVENTS_LOG` is read through the module (`config.EVENTS_LOG`
at call time), never bound as a module-level name — otherwise this module would
have to be registered in `tests/conftest.py`'s `_EVENTS_LOG_MODULES` or every
test here would write into the real cockpit's log.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from . import config, task_scope, typesafe_bridge
from .task_scope import TIER_ORDER, ScopeDecision


def _stored_api_key() -> str:
    """The Settings page's stored key — `typesafe_bridge.api_key()` falls back
    to this when no env var is set. Registered here (not in the bridge) so the
    bridge stays a pure stdlib leaf."""
    from . import core_v2_settings

    return core_v2_settings.load_typesafe_api_key()


typesafe_bridge.set_key_loader(_stored_api_key)

MODES: tuple[str, ...] = ("off", "shadow", "on")
DEFAULT_MODE = "off"
ENV_MODE = "TAKKUB_DECIDE_MODE"

# Below this, the model's own answer is not trusted on its own. The floor is not
# "ask the user" (this runs inside `assign`, with no user in the loop) but "take
# the more cautious of the two tiers": under-sizing is the expensive direction —
# a deep task sized tiny skips qa/reviewer entirely (#585) — while over-sizing
# only wastes ceremony. 0.70 sits well under every confidence observed on the
# 2026-09-21 probe (0.89-1.00), so it fires on genuine ambiguity, not routinely.
CONFIDENCE_FLOOR = 0.70

# Diagnostics only: shadow rows stop being appended past this size rather than
# racing `orchestrator_text._append_event_lines` for the rotation.
_EVENTS_LOG_SOFT_CAP = 2 * 1024 * 1024

# The rubric is a restatement of `task_scope`'s own module docstring, so the
# model is judged against the SAME contract the regex table implements. Two
# behaviours are spelled out because they are exactly where the regex table
# fails: intent beats vocabulary (read-only work is never deep), and a
# self-description like "แค่"/"just" does not make a task tiny (#670).
SCOPE_QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": (
        "A Lead agent must pick a work-budget tier for this task before handing it to a "
        "coding agent. The task text may be Thai, English or mixed. Judge what the task "
        "will DO, not which words appear in it: a task that only reads, audits, queries "
        "or reports and writes nothing is never 'deep' however risky its subject sounds, "
        "and a task is not 'tiny' just because it calls itself small ('just', 'แค่'). "
        "Ignore risky words that appear only in a prohibition ('ห้ามแตะ auth')."
    ),
    "criteria": {
        "tiny": (
            "Small, low-risk edit: a typo, wording, label, colour, padding, one config "
            "value, a one-line change, or a single pinpointed spot. About 20 lines or less."
        ),
        "normal": (
            "The default. Ordinary feature work or a bug fix touching 1-5 files, and also "
            "any read-only investigation or audit no matter how large its subject."
        ),
        "deep": (
            "High risk or broad blast radius AND the task actually changes it: schema, "
            "migration, auth, security, secrets/tokens, crypto, payments, dependencies, "
            "lockfiles, infra, CI/CD, cross-module refactor, project-wide rename."
        ),
    },
}

READ_ONLY_QUESTION: dict[str, Any] = {
    "type": "noul",
    "instructions": (
        "The task only reads, inspects, audits, queries or reports. It changes no file "
        "and writes no code."
    ),
}


def mode() -> str:
    """Active mode: env override first, then the persisted setting, then
    :data:`DEFAULT_MODE`. An unrecognised value in either place reads as the
    default — a typo must not silently start spending money."""
    raw = (os.environ.get(ENV_MODE) or "").strip().lower()
    if raw in MODES:
        return raw
    try:
        from . import core_v2_settings

        stored = core_v2_settings.load_decide_mode()
    except Exception:
        return DEFAULT_MODE
    return stored if stored in MODES else DEFAULT_MODE


def _on_qt_main_thread() -> bool:
    """True only when we are executing on the cockpit's GUI thread.

    `cli.assign` runs in a throwaway CLI process where an 800 ms round trip is
    just latency, but `orchestrator._do_assign` and `routing_planner.classify`
    can run on the Qt main thread, where the same call is a visible freeze — the
    exact failure mode that cost us #640 (25 s boot sweep) and the Lead typing
    lag. So "on" degrades to shadow behaviour there rather than blocking: the
    regex answer is used and the model is asked on `bg_pool` anyway, which still
    feeds the comparison log.

    Never imports PyQt: if `PyQt6.QtCore` is not already loaded there is no GUI
    thread to be on, so a headless/CLI caller answers False without cost.
    """
    try:
        qtcore = __import__("sys").modules.get("PyQt6.QtCore")
        if qtcore is None:
            return False
        app = qtcore.QCoreApplication.instance()
        if app is None:
            return False
        return bool(qtcore.QThread.currentThread() == app.thread())
    except Exception:
        return False


# Shadow mode logs from `bg_pool`'s 4 workers at once, and Python's append mode
# on Windows is seek-to-end-then-write, not an atomic O_APPEND: measured
# 2026-09-21, 8 threads x 200 lines produced 1483/1600 lines — no corruption,
# just silently dropped rows, which would have quietly understated whatever
# disagreement rate the shadow comparison reported. `orchestrator_text` solved
# the same race with a single writer thread; one lock is enough here because
# every writer in this module is in-process (a second cockpit process appending
# to the same log is a pre-existing condition for every events.log writer).
_log_lock = threading.Lock()


def _log(event: str, **details: Any) -> None:
    try:
        target = config.EVENTS_LOG
        try:
            if target.exists() and target.stat().st_size > _EVENTS_LOG_SOFT_CAP:
                return
        except OSError:
            pass
        line = json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **details},
            ensure_ascii=False,
        )
        with _log_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def _ask_jev(task_text: str) -> tuple[str, float, float | None, float, str] | None:
    """``(tier, confidence, read_only, elapsed_ms, model)`` or ``None``.

    Both questions ride in one request — a second call would cost roughly what
    the first did for a signal used only to explain the decision.
    """
    from . import typesafe_bridge

    reply = typesafe_bridge.ask(
        task_text,
        {"scope": SCOPE_QUESTION, "read_only": READ_ONLY_QUESTION},
    )
    if reply is None:
        return None
    answer = reply.answers.get("scope")
    if answer is None or answer.kind != "choice" or answer.value not in task_scope.SCOPE_TIERS:
        return None
    read_only = reply.answers.get("read_only")
    return (
        str(answer.value),
        # A choice always carries a confidence today. If one ever arrives without
        # it, "unknown" must read as 0.0 (untrusted -> the safety floor decides),
        # never as 1.0: defaulting to certainty would let a schema change hand
        # the model unchecked authority over the tier.
        float(answer.confidence) if answer.confidence is not None else 0.0,
        float(read_only.value) if read_only is not None else None,
        reply.elapsed_ms,
        reply.model,
    )


def _shadow_probe(task_text: str, local: ScopeDecision) -> None:
    """Runs on `bg_pool`: ask Jev, log the comparison, change nothing."""
    from . import typesafe_bridge

    got = _ask_jev(task_text)
    if got is None:
        _log(
            "decide_shadow",
            mode="shadow",
            local=local.scope,
            error=typesafe_bridge.last_error(),
        )
        return
    tier, conf, read_only, elapsed_ms, model = got
    _log(
        "decide_shadow",
        mode="shadow",
        local=local.scope,
        jev=tier,
        confidence=round(conf, 3),
        read_only=None if read_only is None else round(read_only, 3),
        agree=tier == local.scope,
        ms=round(elapsed_ms),
        model=model,
        task=task_text[:200],
    )


def scope(task_text: str) -> ScopeDecision:
    """Drop-in replacement for `task_scope.classify` that honours :func:`mode`.

    Always returns a :class:`~.task_scope.ScopeDecision`; never raises, never
    blocks longer than `typesafe_bridge.DEFAULT_TIMEOUT`, and in ``off`` mode is
    `task_scope.classify` with one dict lookup of overhead.
    """
    local = task_scope.classify(task_text)
    active = mode()
    if active == "off" or not (task_text or "").strip():
        return local

    if active == "shadow" or _on_qt_main_thread():
        try:
            from . import bg_pool

            bg_pool.submit(_shadow_probe, task_text, local)
        except Exception:
            pass
        if active == "on":
            _log("decide_scope", mode="on", chosen=local.scope, source="local", reason="qt-main")
        return local

    # mode == "on"
    from . import typesafe_bridge

    if not typesafe_bridge.available():
        _log("decide_scope", mode="on", chosen=local.scope, source="local", reason="no-key")
        return ScopeDecision(
            local.scope,
            f"{local.reason} [decide=on แต่ยังไม่มี API key — ใช้ตารางเดิม]",
        )

    got = _ask_jev(task_text)
    if got is None:
        error = typesafe_bridge.last_error()
        _log("decide_scope", mode="on", chosen=local.scope, source="local", error=error)
        return ScopeDecision(local.scope, f"{local.reason} [jev ไม่ตอบ: {error} — ใช้ตารางเดิม]")

    tier, conf, read_only, elapsed_ms, model = got
    ro_note = ""
    if read_only is not None and read_only >= 0.5:
        ro_note = f", อ่านเฉยๆ {read_only:.0%}"

    if conf >= CONFIDENCE_FLOOR:
        chosen, source = tier, "jev"
        reason = f"jev {model}: {tier} (มั่นใจ {conf:.0%}{ro_note})"
    else:
        # Safety floor: take whichever tier is more cautious, and say so.
        chosen = tier if TIER_ORDER[tier] >= TIER_ORDER[local.scope] else local.scope
        source = "floor"
        reason = (
            f"jev {model} ไม่มั่นใจ ({tier} ที่ {conf:.0%}{ro_note}) "
            f"— ยกไปทางที่ปลอดภัยกว่าระหว่าง jev/{tier} กับ ตารางเดิม/{local.scope}: {chosen}"
        )

    _log(
        "decide_scope",
        mode="on",
        chosen=chosen,
        source=source,
        local=local.scope,
        jev=tier,
        confidence=round(conf, 3),
        ms=round(elapsed_ms),
        model=model,
    )
    return ScopeDecision(chosen, reason)


def stats() -> dict[str, Any]:
    """Spend meter for the (not yet built) Settings page, plus the active mode."""
    from . import typesafe_bridge

    data = typesafe_bridge.stats()
    data["mode"] = mode()
    data["key_present"] = typesafe_bridge.available()
    return data
