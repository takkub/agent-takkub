"""Shared token-count estimator — issue #516 F2.

Replaces the 4 independent `_CHARS_PER_TOKEN = 4` copies that used to live in
`core.context_sources.base`, `core.brain.context_builder`, `core.brain.
retrieval`, and `boot_context`. Real measurement
(`docs/audit/2026-09-07-boot-context.md` §2) showed plain chars//4 under-
counts this repo's own content by ~3.5x, because this codebase's role files,
CLAUDE.md, and memory are majority Thai — Thai script tokenizes far denser
than English (no spaces to anchor word-piece boundaries, and each Claude BPE
token typically covers 1-2 Thai codepoints, not ~4).

Still a heuristic, not a real tokenizer: weight Thai-script characters and
everything else differently instead of a single flat ratio, which gets
mixed Thai+English content (the norm for this repo) far closer to the one
real calibration point available (a `cache_creation_input_tokens` ground
truth from an actual transcript) than a single global ratio could. Treat
the result as a lower-bound estimate for planning/regression-ratcheting, not
an exact prediction — see the module-level caveat in the callers that
compare it against real usage-ledger numbers.
"""

from __future__ import annotations

import re

# Thai script block (also covers Thai digits/vowels/tone marks). Empirically
# closer to 1 BPE token per 1.1 Thai codepoints for this repo's content
# (docs/audit/2026-09-07-boot-context.md) than the ~4 chars/token that holds
# for English — 0.9 token/char is the calibrated weight for this class.
_THAI_RE = re.compile(r"[฀-๿]")
_THAI_TOKENS_PER_CHAR = 0.9
_OTHER_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Lower-bound token estimate, Thai-weighted.

    Non-Thai characters keep the original chars/4 ratio (English, code,
    punctuation, whitespace); Thai characters are weighted at ~0.9
    tokens/char. Splitting by character class rather than switching a
    single global ratio keeps English-only and code-only callers (most of
    `core.brain`/`core.context_sources`'s existing callers) unaffected.
    """
    if not text:
        return 1
    thai_chars = len(_THAI_RE.findall(text))
    other_chars = len(text) - thai_chars
    tokens = thai_chars * _THAI_TOKENS_PER_CHAR + other_chars / _OTHER_CHARS_PER_TOKEN
    return max(1, int(tokens))
