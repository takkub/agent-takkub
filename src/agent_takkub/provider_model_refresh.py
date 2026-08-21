"""Boot-time model-catalog refresh per provider (user directive 2026-08-20:
"gemini ออก 3.7 แล้วแต่ cockpit ยังรู้จัก/เลือกรุ่นเก่าอยู่").

Runs in the SAME boot phase as the binary update (`provider_update.py`),
gated by the exact same eligibility rule: only a provider that is both
installed AND enabled is ever probed (`provider_update.eligibility_gap`).

Only ever touches a provider's PINNED model
(`provider_models.model_for(name)` is not None). An unpinned provider
already always rides its own CLI's default model, which is inherently
fresh — nothing to refresh. `provider_models.set_model()` is the exact same
persistence Settings uses, so a bump here is indistinguishable from the user
picking the newer model themselves; Settings can still override it manually
afterward.

Discovery is implemented ONLY where BOTH (a) an official CLI subcommand for
listing models is confirmed to exist, AND (b) its real output was captured
and verified against the actual installed binary on 2026-08-20 — never a
guessed output format or a hardcoded "latest model id" table (a hardcoded
table would go stale the exact same way this feature exists to fix).
See docs/audit/2026-08-20-boot-update-policy.md for the full per-provider
proof/gap table, including why opencode (mechanism confirmed, real output
captured) was deliberately NOT implemented this wave: its `models` command
lists 75+ third-party backends, unordered by recency, spanning vendors this
cockpit has no way to independently verify freshness for — a wrong bump
there would silently redirect a role to a different, unintended LLM
backend, which is worse than the "stays as configured" status quo every
other gap in this module defers to.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from ._win_console import SUBPROCESS_NO_WINDOW

logger = logging.getLogger(__name__)

STATUS_NO_PIN = "no_pin"  # nothing configured — CLI default is always fresh
STATUS_UP_TO_DATE = "up_to_date"
STATUS_BUMPED = "bumped"
STATUS_UNMATCHED = "unmatched"  # pinned model not found in the current catalog — left alone
STATUS_DISCOVERY_FAILED = "discovery_failed"
STATUS_GAP = "gap"  # no confirmed discovery mechanism for this provider

# Providers with NO confirmed, real-output-verified model-list mechanism this
# wave (mirrors provider_update.NO_UPDATE_MECHANISM_GAPS / pane_env's
# NO_AUTOUPDATE_KNOB_GAPS — same "document instead of guess" policy).
# Flagged to issue #103 (comment posted 2026-08-20).
NO_MODEL_DISCOVERY_GAPS: dict[str, str] = {
    "claude": (
        "no `claude models`-style list subcommand (confirmed via `claude --help`, "
        "2026-08-20); the CLI's own always-fresh alternative is its --model TIER "
        "ALIASES ('opus'/'sonnet'/'fable'/'haiku' always resolve to that tier's "
        "latest release) but resolving an alias to a concrete id requires a real, "
        "billed generation — not run automatically just to check freshness"
    ),
    "kimi": "no models-list subcommand (confirmed via `kimi --help`, 2026-08-20)",
    "cursor": (
        "`agent models` is documented to exist (cursor.com CLI reference) but cursor "
        "is not installed on any machine this feature was built against — output "
        "format unverified, so no parser was written against a guess"
    ),
    "opencode": (
        "`opencode models [provider]` exists and its real output WAS captured "
        "(2026-08-20) — deliberately not wired up: it lists 75+ third-party model "
        "backends, alphabetically (not recency-ordered like agy's), spanning "
        "vendors this cockpit can't independently verify freshness for. A wrong "
        "bump would silently redirect a role to an unintended LLM backend"
    ),
}


@dataclass(frozen=True)
class ModelRefreshOutcome:
    provider: str
    status: str
    detail: str = ""


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_NO_WINDOW,
    )


def _discover_gemini_models_json(binary: str) -> list[str] | None:
    """Run `agy --output-format json models` and return model ids in the
    CLI's own listing order.

    Real captured shape (agy 1.1.15, confirmed installed on dev machine,
    2026-08-20)::

        {"conversation_id": "", "status": "SUCCESS",
         "response": "gemini-3.7-flash-high\\tGemini 3.7 Flash (High)\\n...",
         "duration_seconds": 0, "num_turns": 0,
         "usage": {"input_tokens": 0, ...},
         "command": {"name": "models",
                      "data": {"models": [
                          {"id": "gemini-3.7-flash-high",
                           "label": "Gemini 3.7 Flash (High)"},
                          ...]}}}

    `command.data.models` is in the same newest-first-per-family order as
    the text listing (directly observed: identical id sequence in both
    outputs) — the ordering property `_pick_latest_per_family` relies on.

    `--output-format` is a GLOBAL flag, not subcommand-local: it must come
    BEFORE the subcommand (`agy --output-format json models`), not after
    (`agy models --output-format json` fails with "flags provided but not
    defined", confirmed 2026-08-20). An agy older than 1.1.12 (before the
    `models` subcommand supported this flag) also fails the global-flag
    parse the same way agy does for any unrecognized flag: non-zero exit,
    empty stdout (confirmed via `--totally-bogus-flag`, 2026-08-20) — that
    lands here as a clean None, same as a network/auth failure.

    Returns None on ANY shape mismatch — non-zero exit, non-JSON stdout, or
    a missing/malformed `command.data.models` list — never guesses at a
    partial parse. Caller falls back to the text parser.
    """
    try:
        result = _run([binary, "--output-format", "json", "models"])
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "")
        models = payload["command"]["data"]["models"]
        ids = [m["id"] for m in models if isinstance(m, dict) and m.get("id")]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None
    return ids or None


def _discover_gemini_models_text(binary: str) -> list[str] | None:
    """Run `agy models` (no output-format flag) and return model ids in the
    CLI's own listing order.

    Real captured output (agy, 2026-08-20)::

        Fetching available models...
        gemini-3.7-flash-high\tGemini 3.7 Flash (High)
        gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
        ...
        gemini-3.6-flash-high\tGemini 3.6 Flash (High)
        ...

    Newer releases are listed BEFORE older ones within the same family
    (3.7 before 3.6 before 3.5, directly observed) — this is the ordering
    property `_pick_latest_per_family` below relies on, not an assumption.
    Lines without a tab (the "Fetching..." status line, blank lines) are
    skipped. Returns None on any subprocess failure — caller reports
    `discovery_failed`, never treats an empty/failed run as "no models".

    Fallback path for agy builds older than 1.1.12 (no `--output-format`
    support on the `models` subcommand) — see `_discover_gemini_models_json`.
    """
    try:
        result = _run([binary, "models"])
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    ids: list[str] = []
    for line in (result.stdout or "").splitlines():
        if "\t" not in line:
            continue
        model_id = line.split("\t", 1)[0].strip()
        if model_id:
            ids.append(model_id)
    return ids or None


def _discover_gemini_models(binary: str) -> list[str] | None:
    """JSON path first (structured, immune to display-text drift); falls
    back to the text parser when the JSON path fails — old agy without
    `--output-format` support on `models`, or any other shape mismatch.
    Logs which path actually produced the result."""
    ids = _discover_gemini_models_json(binary)
    if ids is not None:
        logger.info("gemini model discovery: used json path (%d models)", len(ids))
        return ids
    ids = _discover_gemini_models_text(binary)
    if ids is not None:
        logger.info("gemini model discovery: used text fallback (%d models)", len(ids))
        return ids
    logger.warning("gemini model discovery: both json and text paths failed")
    return None


def _discover_codex_models(binary: str) -> list[str] | None:
    """Read codex's own `models_cache.json` and return the listable slugs,
    newest version first.

    codex still ships no models-list subcommand (`codex --help`, re-confirmed
    2026-08-21) — but the CLI maintains this cache itself, and
    `settings_window._MODELS_BY_PROVIDER` already documents reading exactly
    these `slug` fields by hand. This wires that same source up so the one
    provider carrying real, concrete pins on this team stops being the one
    provider nothing can refresh.

    Real captured shape (codex 0.149.0, dev box, 2026-08-21)::

        {"fetched_at": "2026-08-21T04:29:59Z", "etag": "...",
         "client_version": "0.149.0",
         "models": [{"slug": "gpt-5.6-sol", "visibility": "list",
                     "priority": 1, ...}, ...]}

    `visibility` is honoured rather than inferred: `gpt-reserve` and
    `codex-auto-review` both ship as `"hide"` and must never become a bump
    target. `priority` is NOT used for ordering — it ranks prominence, not
    recency (sol/terra/luna are 1/2/3 within the same release), so a future
    cache could list a newer version below an older one. The version token
    in the slug is the actual recency signal, so that is what orders this
    list; `sorted` is stable, so same-version variants keep cache order.

    `binary` is unused (the cache is a plain file) but kept in the signature
    so every discovery function has the one shape `refresh_provider_model`
    dispatches to.
    """
    from .codex_helper import codex_home

    try:
        raw = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
        entries = raw["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    ids = [
        m["slug"]
        for m in entries
        if isinstance(m, dict) and m.get("slug") and m.get("visibility") != "hide"
    ]
    if not ids:
        return None
    return sorted(ids, key=_version_tuple, reverse=True)


def _version_tuple(model_id: str) -> tuple[int, ...]:
    """The slug's version token as a comparable tuple, e.g.
    ``gpt-5.6-terra`` -> ``(5, 6)``. No version token (``gpt-reserve``)
    sorts last as ``()`` — those are dropped by `_pick_latest_per_family`
    anyway, which needs the same token to build a family key."""
    for part in model_id.split("-"):
        if _looks_like_version(part):
            return tuple(int(seg) for seg in part.split(".") if seg != "")
    return ()


def _family_key(model_id: str) -> tuple[str, ...] | None:
    """Strip the version-number token from a model id, e.g.
    ``gemini-3.7-flash-medium`` -> ``("gemini", "flash", "medium")`` — the
    same family as ``gemini-3.5-flash-medium``, distinct from the
    ``-high``/``-low`` tier (a real, separate variant, not a version) and
    from ``-pro`` (a different product line). Returns None when no token
    looks like a version number (id doesn't match the confirmed shape).

    Provider-agnostic despite being written for gemini first: codex's slugs
    have the identical shape, where it separates ``gpt-5.6-terra`` from
    ``gpt-5.6-sol`` (sibling variants of one release, not versions of each
    other) exactly the way it separates gemini's ``-high``/``-low``."""
    parts = model_id.split("-")
    family = [p for p in parts if not _looks_like_version(p)]
    if len(family) == len(parts):
        return None  # no version token found — not this format
    return tuple(family)


def _looks_like_version(token: str) -> bool:
    if not token:
        return False
    return all(seg.isdigit() for seg in token.split(".") if seg != "") and any(
        c.isdigit() for c in token
    )


def _pick_latest_per_family(ids: list[str]) -> dict[tuple[str, ...], str]:
    """First occurrence per family wins — `ids` is already newest-first
    within a family (see `_discover_gemini_models`'s docstring)."""
    latest: dict[tuple[str, ...], str] = {}
    for model_id in ids:
        fam = _family_key(model_id)
        if fam is not None and fam not in latest:
            latest[fam] = model_id
    return latest


# provider -> the name of its discovery function in this module. Every one of
# them returns model ids newest-first within a family, or None when its
# channel failed / returned a shape it does not recognise. Held by NAME, not
# by reference: a dict of function objects binds at import time, so a
# reference captured here would shadow the module attribute the rest of the
# codebase (and every test) patches.
_DISCOVERY: dict[str, str] = {
    "gemini": "_discover_gemini_models",
    "codex": "_discover_codex_models",
}


def _discovery_for(provider: str) -> Callable[[str], list[str] | None]:
    return globals()[_DISCOVERY[provider]]


def _pins_for(provider: str) -> list[tuple[str | None, str]]:
    """Every pin this provider carries as ``(role, model)``, role ``None``
    for the provider-level default.

    BOTH stores matter, and the role store matters more: `provider_models`
    only holds the fallback for a role that never overrode it, while
    `role_models` (role-models.json) is where a team's actual per-role models
    live. The first wave of this feature read only the provider store, so on
    a real config — every pin in role-models.json, none at provider level —
    the boot refresh reported NO_PIN and did nothing, every single boot,
    while the roles stayed frozen on whatever they were pinned to.
    """
    from . import provider_models, role_models

    pins: list[tuple[str | None, str]] = []
    provider_pin = provider_models.model_for(provider)
    if provider_pin:
        pins.append((None, provider_pin))
    pins.extend(
        (role, entry["model"])
        for role, entry in sorted(role_models.all_models().items())
        if entry.get("provider") == provider and entry.get("model")
    )
    return pins


def _apply_pin(provider: str, role: str | None, model: str) -> None:
    """Persist a bumped pin back to whichever store it came from — the same
    calls Settings makes, so a bump is indistinguishable from the user
    picking the newer model themselves and Settings can still override it
    afterward (user directive 2026-08-20: "Settings ยัง override ได้"). No
    pin-intent tracking exists, so every stale pin is bumped unconditionally,
    per that same directive's answer for a deliberately-pinned model."""
    from . import provider_models, role_models

    if role is None:
        provider_models.set_model(provider, model)
    else:
        role_models.set_model(role, provider, model)


def _refresh_pins(provider: str, binary: str) -> ModelRefreshOutcome:
    discover = _discovery_for(provider)
    pins = _pins_for(provider)
    if not pins:
        # Nothing pinned anywhere: the CLI rides its own default, which is
        # inherently fresh — no reason to spend a discovery call.
        return ModelRefreshOutcome(provider, STATUS_NO_PIN)

    ids = discover(binary)
    if ids is None:
        return ModelRefreshOutcome(provider, STATUS_DISCOVERY_FAILED, "model discovery failed")
    latest_by_family = _pick_latest_per_family(ids)

    bumped: list[str] = []
    unmatched: list[str] = []
    for role, pinned in pins:
        label = pinned if role is None else f"{role}: {pinned}"
        fam = _family_key(pinned)
        latest = latest_by_family.get(fam) if fam is not None else None
        if latest is None:
            # Pin doesn't parse as a versioned id, or its family is gone from
            # the catalog — left exactly as configured, never guessed at.
            unmatched.append(label)
        elif latest != pinned:
            _apply_pin(provider, role, latest)
            bumped.append(f"{label} -> {latest}")

    # One outcome covers every pin: a bump anywhere is the headline, since
    # that is the only status the boot window surfaces to the user at all.
    if bumped:
        return ModelRefreshOutcome(provider, STATUS_BUMPED, " · ".join(bumped))
    if unmatched:
        return ModelRefreshOutcome(provider, STATUS_UNMATCHED, " · ".join(unmatched))
    return ModelRefreshOutcome(provider, STATUS_UP_TO_DATE, " · ".join(p for _, p in pins))


def refresh_provider_model(name: str, binary: str) -> ModelRefreshOutcome:
    """Refresh *name*'s pinned models if they're stale within their own line.

    Covers BOTH the provider-level pin and every role pinned to this
    provider (see `_pins_for`).

    Caller MUST have already confirmed eligibility (installed + enabled) —
    mirrors `provider_update.update_provider`'s contract; this function does
    not re-check `provider_state` itself, since it is only ever called from
    the same worker that already ran the binary-update eligibility check.
    """
    if name in _DISCOVERY:
        return _refresh_pins(name, binary)
    gap = NO_MODEL_DISCOVERY_GAPS.get(name, "no discovery mechanism wired for this provider")
    return ModelRefreshOutcome(name, STATUS_GAP, gap)
