"""#591: pane provider/model display — pure formatting/decision logic, no Qt.

A pane's header and tab label show which provider + model it is actually
running. Precedence: the CLI's own live-reported model (token_meter's
usage["model"] for claude/codex, or `PtySession.current_model_label("gemini")`
for gemini) wins over what `spawn_engine.spawn()` resolved at launch time —
a live report can drift from the spawn-time value (quota downgrade,
mid-session `/model`), and surfacing that drift silently was the actual gap
#591 exists to close. opencode/kimi/cursor never report a live model back to
the cockpit (their CLIs don't expose one) — callers pass `live_model=None`
for those and the tooltip says so explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Providers this module will ever trust a `live_model` argument for. Every
# other registered provider (opencode/kimi/cursor today) never reports one —
# the tooltip must say "CLI doesn't report a model", not silently read the
# same as "no live sample has landed yet".
LIVE_MODEL_PROVIDERS = frozenset({"claude", "codex", "gemini"})

# Roles/pseudo-providers spawned by this engine that carry no model concept
# at all (e.g. a raw shell pane) — never show a display for these.
_NO_MODEL_PROVIDERS = frozenset({"shell"})

_ONE_M_SUFFIX_RE = re.compile(r"\s*\[1m\]\s*$", re.IGNORECASE)
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _model_families_match(spawn_short: str, live_short: str) -> bool:
    """True when `live_short` is the same model family `spawn_short` names,
    or just a more specific spelling of it (#591 follow-up).

    Settings/role config store a bare Claude tier alias — "sonnet", "opus",
    "haiku" — while the live CLI always reports the concrete model id it
    actually resolved that alias to ("sonnet-5", "opus-5", ...). Comparing
    those for exact equality flagged every alias-pinned pane as a mismatch
    even though nothing had actually drifted. A live id counts as the same
    family when it starts with the spawn alias followed by end-of-string or
    a "-" (word boundary) — so "sonnet" matches "sonnet-5" but not
    "sonnet-4-6" is still allowed to *not* match when spawn itself already
    names a specific version ("sonnet-5" vs "sonnet-4-6" stays a mismatch,
    since "sonnet-4-6" does not start with "sonnet-5").
    """
    if spawn_short == live_short:
        return True
    if live_short.startswith(spawn_short):
        rest = live_short[len(spawn_short) :]
        if not rest or rest[0] == "-":
            return True
    return False


def shorten_model_name(model: str | None) -> str:
    """Best-effort short label for a model id/name: drop the "claude-"
    prefix and a trailing date stamp or "[1m]" context-window suffix; every
    other provider's id (or gemini's free-text footer label) passes through
    unchanged. Empty/None -> "".
    """
    if not model:
        return ""
    text = model.strip()
    text = _ONE_M_SUFFIX_RE.sub("", text)
    text = _DATE_SUFFIX_RE.sub("", text)
    if text.startswith("claude-"):
        text = text[len("claude-") :]
    return text


@dataclass(frozen=True)
class ProviderModelDisplay:
    short_text: str  # e.g. "claude · sonnet-5" — header text / tab suffix source
    tooltip: str
    mismatch: bool  # True when the live-reported model differs from spawn-time


def resolve_provider_model_display(
    *,
    provider: str | None,
    spawn_model: str | None,
    spawn_effort: str | None = None,
    spawn_explicit: bool = False,
    live_model: str | None = None,
) -> ProviderModelDisplay | None:
    """Decide what a pane's header/tab should show for provider + model
    (#591). Returns None when this pane never spawned, or spawned something
    with no model concept (e.g. a shell pane) — nothing to show either way.
    """
    if not provider or provider in _NO_MODEL_PROVIDERS:
        return None

    reports_live = provider in LIVE_MODEL_PROVIDERS
    effective = live_model or spawn_model or ""
    short_model = shorten_model_name(effective)
    short_text = f"{provider} · {short_model}" if short_model else provider

    mismatch = (
        bool(live_model)
        and bool(spawn_model)
        and not _model_families_match(
            shorten_model_name(spawn_model), shorten_model_name(live_model)
        )
    )
    if mismatch:
        short_text += " ⚠"

    if not reports_live:
        source = "ตามที่ตั้งตอนเปิด — CLI นี้ไม่รายงาน model"
    elif live_model:
        source = "ตาม CLI จริง"
    elif spawn_explicit:
        source = "--model ที่ระบุ"
    else:
        source = "ตามที่ตั้งตอนเปิด"

    tooltip_lines = [
        f"provider: {provider}",
        f"model: {effective or '(ค่าเริ่มต้น)'}",
    ]
    if spawn_effort:
        tooltip_lines.append(f"effort: {spawn_effort}")
    tooltip_lines.append(f"ที่มา: {source}")
    if mismatch:
        tooltip_lines.append(
            f"ตั้งไว้ {shorten_model_name(spawn_model)} แต่รันจริง {shorten_model_name(live_model)}"
        )
    tooltip = "\n".join(tooltip_lines)

    return ProviderModelDisplay(short_text=short_text, tooltip=tooltip, mismatch=mismatch)


def tab_label_with_model(
    base_label: str, display: ProviderModelDisplay | None, max_len: int = 24
) -> str:
    """`base_label` (e.g. "Backend", "QA") + " · <short model>" for the pane
    tab strip, truncated to `max_len` chars so a long model id can't blow
    out tab width (#591). `display=None` (no spawn yet, or a no-model
    provider) leaves the base label untouched.
    """
    if display is None or not display.short_text:
        return base_label
    # short_text is "<provider> · <model>[ ⚠]" — the tab only wants the
    # model half (+ warning marker) appended to the role label.
    model_part = display.short_text.split("·", 1)[-1].strip()
    if not model_part:
        return base_label
    text = f"{base_label} · {model_part}"
    if len(text) <= max_len:
        return text
    keep = max_len - len(base_label) - len(" · ") - 1  # trailing "…"
    if keep <= 1:
        return base_label
    return f"{base_label} · {model_part[:keep]}…"
