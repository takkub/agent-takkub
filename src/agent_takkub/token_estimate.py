"""Shared token-count estimator — issue #516 F2 / #516b merge.

Replaces the 4 independent `_CHARS_PER_TOKEN = 4` copies that used to live in
`core.context_sources.base`, `core.brain.context_builder`, `core.brain.
retrieval`, and `boot_context`, *and* the standalone marketplace/skill cost
estimator `pane_tools_dialog`/`settings_window` used for Tools-page cost
badges — both landed independently in #516 and were unified here on merge.

Real measurement (`docs/audit/2026-09-07-boot-context.md` §2) showed plain
chars//4 undercounts this repo's own content by ~3.5x, because this
codebase's role files, CLAUDE.md, and memory are majority Thai — Thai script
tokenizes far denser than English (no spaces to anchor word-piece
boundaries, and each Claude BPE token typically covers 1-2 Thai codepoints,
not ~4). Still a heuristic, not a real tokenizer: weight Thai-script
characters and everything else differently instead of a single flat ratio,
which gets mixed Thai+English content (the norm for this repo) far closer to
the one real calibration point available (a `cache_creation_input_tokens`
ground truth from an actual transcript) than a single global ratio could.

Non-Thai ratio is chars/4, not chars/3.8: kept the exact pre-#516 chars/4
convention for the non-Thai term so the 4 existing `core.*`/`boot_context`
call sites — and the boot-context ceiling baseline
(`docs/audit/boot-context-baseline.json`) already calibrated against it —
see zero behavior change from this merge; only the previously-uncounted
Thai weighting is new. The marketplace/skill cost badges (`pane_tools_dialog.
marketplace_token_costs`, `settings_window`'s Tools-page labels) are a
coarser, purely comparative display and don't need a different ratio to stay
useful. Treat the result as a lower-bound estimate for planning/regression-
ratcheting, not an exact prediction — see the module-level caveat in the
callers that compare it against real usage-ledger numbers.
"""

from __future__ import annotations

import pathlib
import re

# Thai script block (also covers Thai digits/vowels/tone marks). Empirically
# closer to 1 BPE token per 1.1 Thai codepoints for this repo's content
# (docs/audit/2026-09-07-boot-context.md) than the ~4 chars/token that holds
# for English — 0.9 token/char is the calibrated weight for this class.
_THAI_RE = re.compile(r"[฀-๿]")
_THAI_TOKENS_PER_CHAR = 0.9
_OTHER_CHARS_PER_TOKEN = 4

# Text-ish files worth counting toward a plugin/skill's boot cost — anything
# else under a plugin dir (binaries, images, lockfiles) never reaches a
# pane's context.
_COST_SUFFIXES = frozenset({".md", ".json", ".py", ".js", ".ts", ".sh", ".txt", ".yaml", ".yml"})


def estimate_tokens(text: str) -> int:
    """Lower-bound token estimate, Thai-weighted.

    Non-Thai characters keep the original chars/4 ratio (English, code,
    punctuation, whitespace); Thai characters are weighted at ~0.9
    tokens/char. Splitting by character class rather than switching a
    single global ratio keeps English-only and code-only callers (most of
    `core.brain`/`core.context_sources`'s existing callers) unaffected.
    Floors at 1 for any non-empty-or-not text so a measured category never
    displays as a misleading "0 tok".
    """
    if not text:
        return 1
    thai_chars = len(_THAI_RE.findall(text))
    other_chars = len(text) - thai_chars
    tokens = thai_chars * _THAI_TOKENS_PER_CHAR + other_chars / _OTHER_CHARS_PER_TOKEN
    return max(1, int(tokens))


def estimate_file_tokens(path: pathlib.Path) -> int:
    """`estimate_tokens` over *path*'s content, 0 on any read failure."""
    try:
        return estimate_tokens(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return 0


def estimate_dir_tokens(root: pathlib.Path, *, max_files: int = 200) -> int:
    """Sum `estimate_file_tokens` over every text file under *root*
    (recursive) — a plugin/marketplace's total boot cost (SKILL.md + hooks).
    ``max_files`` bounds the walk so one oversized plugin cache can't stall
    the Tools page."""
    if not root.is_dir():
        return 0
    total = 0
    count = 0
    for p in root.rglob("*"):
        if count >= max_files:
            break
        if p.is_file() and p.suffix.lower() in _COST_SUFFIXES:
            total += estimate_file_tokens(p)
            count += 1
    return total


def format_tokens(n: int) -> str:
    """Compact display form, e.g. ``~1.2k tok`` / ``~340 tok``."""
    if n >= 1000:
        return f"~{n / 1000:.1f}k tok"
    return f"~{n} tok"
