"""Auto-open a GitHub issue when the cockpit itself hits an unhandled exception.

Fires only for exceptions inside the cockpit's own process — see
``app.py::_log_unhandled`` (sys.excepthook / threading.excepthook /
unraisablehook). This is not a diagnostic tool for the *user's* project code.

Dedup + rate-cap state lives in ``DATA_HOME/auto_issue_dedup.json`` so a
crash loop can't spam GitHub: the same signature is filed at most once per
24h, and no more than 5 auto-issues (any signature) go out per rolling 24h.
An in-memory mirror backs both caps so a crash storm or a broken disk never
degrades into "file every crash" (see ``_recent`` / ``_fired_mem`` /
``_signatures_mem`` below).

The target repo is public (takkub/agent-takkub), so title/body are scrubbed
of the caller's home directory and redacted for token-shaped substrings
before anything is sent — see ``_scrub_home`` / ``_redact``.

``capture_cockpit_crash`` is a no-op in any test/CI process — see
``_auto_issue_suppressed`` — so a pytest run (which imports ``app.py`` and
therefore installs the same ``sys.excepthook``) can never file a real issue
against the public repo (#188).
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from . import __version__, issues
from .config import DATA_HOME

_DEDUP_PATH = DATA_HOME / "auto_issue_dedup.json"
_COOLDOWN_SECONDS = 24 * 60 * 60
_RATE_CAP = 5
_RATE_WINDOW_SECONDS = 24 * 60 * 60
_MAX_TB_CHARS = 8000
_MAX_MSG_CHARS = 2000

_lock = threading.Lock()

# Module-level seam so tests can patch just this call site instead of
# process-wide threading.Thread (which would also swap out PyQt/pytest
# threads for the duration of the test).
_spawn = threading.Thread

# Fast in-process short-circuit: rejects a duplicate signature before a
# thread is even spawned, so a GC-triggered exception storm can't burn
# thousands of threads on work the dedup file would reject anyway.
_recent: dict[str, float] = {}

# In-memory mirror of the on-disk state, kept in sync on every successful
# worker pass. Used as a fallback source when persistence is broken (e.g.
# DATA_HOME unwritable) so a disk failure degrades to "still capped for this
# process run" instead of "cap silently disabled forever".
_fired_mem: list[float] = []
_signatures_mem: dict[str, float] = {}
_cap_blocked_until: float = 0.0
_persist_broken = False

# Keyword vocabulary mirrors shared_dev_tools.py's _SECRET_KEY_PARTS
# (token/secret/key/password/bearer/credential), widened with passwd/authtoken
# to also cover the argv-repr and env-var forms this module actually sees.
_REDACT_PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?:--?[\w-]*)?(?:token|secret|api[_-]?key|password|passwd|bearer|credential|authtoken)"
        r"[\w-]*['\"\s,:=]+[^\s'\",\]]+",
        re.IGNORECASE,
    ),
    # Basic-auth URL creds (https://user:pass@host/...) — no keyword nearby.
    re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+@"),
    # Catch-all: 20+ char token-shaped run, low enough to cover
    # secrets.token_urlsafe(16) (22 chars) without a nearby keyword.
    re.compile(r"\b[A-Za-z0-9+/_-]{20,}\b"),
]


def _redact(text: str) -> str:
    """Blank out token-shaped substrings before text reaches a public repo."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _scrub_home(text: str) -> str:
    """Replace the caller's home directory with ``~`` (OS username leak).

    Matches case-insensitively and against both path-separator forms —
    reachable in this repo via ``.as_posix()`` (19 call sites) and
    ``gemini_helper.py``'s lowercased posix cwd — so a lowercased or
    forward-slash home path doesn't survive a plain ``str.replace``.
    """
    candidates = []
    try:
        candidates.append(str(Path.home()))
    except OSError:
        pass
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(userprofile)

    for base in candidates:
        for variant in {base, base.replace("\\", "/")}:
            if variant:
                text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)
    return text


def _signature(exc_type: type[BaseException], exc_tb) -> str:
    """Short signature: exception class + last agent_takkub frame (file:line).

    Plain, readable key (e.g. ``ValueError:app.py:57``) — not hashed, so the
    on-disk dedup state stays debuggable at a glance.
    """
    key = exc_type.__name__
    last_own_frame = None
    for frame in traceback.extract_tb(exc_tb):
        if "agent_takkub" in frame.filename.replace("\\", "/"):
            last_own_frame = frame
    if last_own_frame is not None:
        key += f":{Path(last_own_frame.filename).name}:{last_own_frame.lineno}"
    return key


def _load_state() -> dict:
    from .core.storage.v2_authority import read_issue_dedup, v2_authority_enabled

    if v2_authority_enabled():
        v2_state = read_issue_dedup()
        if isinstance(v2_state, dict):
            return v2_state

    if not _DEDUP_PATH.exists():
        return {}
    try:
        with open(_DEDUP_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> bool:
    global _persist_broken
    tmp = _DEDUP_PATH.with_suffix(_DEDUP_PATH.suffix + ".tmp")
    try:
        _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _DEDUP_PATH)
        _persist_broken = False

        from .core.storage.dual_write import dual_write_issue_dedup

        dual_write_issue_dedup(state)
        return True
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if not _persist_broken:
            _persist_broken = True
            try:
                print(
                    f"[auto-issue-capture] state persist failed, "
                    f"falling back to in-memory rate cap: {exc}",
                    file=sys.stderr,
                )
            except Exception:
                pass
        return False


def _prune(mapping: dict, now: float, window: float = _COOLDOWN_SECONDS) -> dict:
    return {
        sig: ts for sig, ts in mapping.items() if isinstance(ts, (int, float)) and now - ts < window
    }


def _auto_issue_suppressed() -> bool:
    """True when auto-issue capture must stay a no-op (test/CI process).

    `app.py` installs `sys.excepthook` at *module import time*, so any
    process that imports `agent_takkub.app` — including a pytest run that
    only imports it transitively — gets its unhandled exceptions routed here
    too (#188: a background pytest process's own `OSError` on a broken
    stdout pipe at shutdown filed a real GitHub issue against the public
    repo). Checked at call time, as a single seam, so
    `test_auto_issue_capture.py`'s tests — which intentionally exercise real
    firing behaviour — can monkeypatch just this function back to `False`
    instead of every one of them going dark.
    """
    if os.environ.get("TAKKUB_SKIP_AUTO_ISSUE_CAPTURE", "").strip() not in ("", "0"):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    # CI is an external convention (GitHub Actions sets it to the literal
    # string "true"), not one of our own TAKKUB_SKIP_* kill switches — so it
    # intentionally stays plain-truthy instead of the "" / "0" convention
    # above.
    if os.environ.get("CI"):
        return True
    if "pytest" in sys.modules:
        return True
    return False


def reserve_signature(sig: str) -> bool:
    """Claim a filing slot for *sig*, or return False if it must be skipped.

    Extracted from `capture_cockpit_crash`'s worker (#297) so the runtime-signal
    reporter in `auto_issue_signals` shares ONE dedup + rate cap with crashes
    rather than getting a second, independently-capped channel — two channels
    would mean a bad hour could file 10 issues while each one believed it was
    obeying a cap of 5.

    Enforces both caps against disk state merged with the in-memory mirror, and
    reserves the slot BEFORE the caller's (slow, networked) create call so two
    racing reporters can't both slip past the cap.
    """
    global _cap_blocked_until
    with _lock:
        now = time.time()
        if now < _cap_blocked_until:
            return False
        disk_state = _load_state()

        signatures = dict(_signatures_mem)
        for s, ts in disk_state.get("signatures", {}).items():
            if isinstance(ts, (int, float)) and (s not in signatures or ts > signatures[s]):
                signatures[s] = ts

        last_filed = signatures.get(sig)
        if isinstance(last_filed, (int, float)) and now - last_filed < _COOLDOWN_SECONDS:
            return False

        fired_raw = list(disk_state.get("fired", [])) + _fired_mem
        fired = sorted(
            {
                ts
                for ts in fired_raw
                if isinstance(ts, (int, float)) and now - ts < _RATE_WINDOW_SECONDS
            }
        )
        if len(fired) >= _RATE_CAP:
            signatures = _prune(signatures, now)
            _save_state({"fired": fired, "signatures": signatures})
            _fired_mem[:] = fired
            _signatures_mem.clear()
            _signatures_mem.update(signatures)
            _cap_blocked_until = fired[0] + _RATE_WINDOW_SECONDS
            return False

        stamp = now if not fired or now > fired[-1] else fired[-1] + 1e-6
        fired.append(stamp)
        signatures[sig] = now
        signatures = _prune(signatures, now)
        _save_state({"fired": fired, "signatures": signatures})
        _fired_mem[:] = fired
        _signatures_mem.clear()
        _signatures_mem.update(signatures)
        return True


def capture_cockpit_crash(exc_type, exc_value, exc_tb, *, source: str) -> None:
    """Fire-and-forget: file an auto-captured GitHub issue for a cockpit crash.

    Dedupes by signature (24h cooldown) and caps total auto-issues at 5 per
    rolling 24h regardless of signature (crash-loop backstop). Runs on a
    background thread so it never blocks the Qt main thread, and swallows
    every failure — this must never raise back into the exception hook that
    calls it (that would defeat the whole point of `_install_exception_guard`).
    """
    if _auto_issue_suppressed():
        return
    try:
        sig = _signature(exc_type, exc_tb)
        now = time.time()
        with _lock:
            if now < _cap_blocked_until:
                return
            last = _recent.get(sig)
            if last is not None and now - last < _COOLDOWN_SECONDS:
                return
            _recent[sig] = now
            pruned = _prune(_recent, now)
            _recent.clear()
            _recent.update(pruned)

        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        tb_text = _redact(_scrub_home(tb_text))[-_MAX_TB_CHARS:]
        exc_name = exc_type.__name__
        exc_msg = _redact(_scrub_home(str(exc_value)))[:_MAX_MSG_CHARS]
    except Exception:
        return

    def _worker() -> None:
        try:
            with _lock:
                now = time.time()
                disk_state = _load_state()

                signatures = dict(_signatures_mem)
                for s, ts in disk_state.get("signatures", {}).items():
                    if isinstance(ts, (int, float)) and (s not in signatures or ts > signatures[s]):
                        signatures[s] = ts

                last_filed = signatures.get(sig)
                if isinstance(last_filed, (int, float)) and now - last_filed < _COOLDOWN_SECONDS:
                    return

                fired_raw = list(disk_state.get("fired", [])) + _fired_mem
                fired = sorted(
                    {
                        ts
                        for ts in fired_raw
                        if isinstance(ts, (int, float)) and now - ts < _RATE_WINDOW_SECONDS
                    }
                )
                if len(fired) >= _RATE_CAP:
                    signatures = _prune(signatures, now)
                    _save_state({"fired": fired, "signatures": signatures})
                    _fired_mem[:] = fired
                    _signatures_mem.clear()
                    _signatures_mem.update(signatures)
                    # This signature never actually fired — free it immediately
                    # instead of leaving it blocked for a full 24h cooldown.
                    _recent.pop(sig, None)
                    global _cap_blocked_until
                    _cap_blocked_until = fired[0] + _RATE_WINDOW_SECONDS
                    return

                # Reserve the slot before the (possibly slow) network call so
                # two crashes racing each other can't both slip past the cap.
                # Keep entries strictly increasing so two reservations inside
                # one time.time() tick (15.6ms on Windows) stay two entries
                # instead of collapsing in the set-dedup above.
                stamp = now if not fired or now > fired[-1] else fired[-1] + 1e-6
                fired.append(stamp)
                signatures[sig] = now
                signatures = _prune(signatures, now)
                _save_state({"fired": fired, "signatures": signatures})
                _fired_mem[:] = fired
                _signatures_mem.clear()
                _signatures_mem.update(signatures)

            title = f"[auto] {exc_name} @ {sig}"
            body = (
                f"Auto-captured unhandled exception (source: {source}).\n\n"
                f"exception: {exc_name}: {exc_msg}\n"
                f"pid: {os.getpid()}\n"
                f"platform: {platform.platform()}\n"
                f"version: {__version__}\n\n"
                f"```\n{tb_text}\n```"
            )
            issues.new_issue(
                title,
                body,
                severity="high",
                tags=["auto-captured"],
                noticed_in="cockpit",
                cockpit_bug=True,
            )
        except Exception:
            pass

    try:
        _spawn(target=_worker, name="auto-issue-capture", daemon=True).start()
    except Exception:
        pass
