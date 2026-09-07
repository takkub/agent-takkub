"""Rough boot-context token-cost estimator (#516).

No live per-pane measurement API exists yet (`takkub doctor --boot-context`
is backend's #516 piece) — this is the documented fallback: read the same
files a pane would actually load (a skill's ``SKILL.md``, a plugin
marketplace's skills/hooks) and estimate tokens with the same formula used
to size the #516/#267 role-file diet: Thai text tokenizes far more
expensively per character than English (no subword boundaries), roughly
0.9 token/Thai-char vs one token per ~3.8 English chars. Swap
``estimate_dir_tokens``/``estimate_file_tokens`` for a real measurement
once the boot-context API lands — callers only need `format_tokens`'s
output shape to stay the same.
"""

from __future__ import annotations

import pathlib
import re

_THAI = re.compile(r"[฀-๿]")

# Text-ish files worth counting toward a plugin/skill's boot cost — anything
# else under a plugin dir (binaries, images, lockfiles) never reaches a
# pane's context.
_COST_SUFFIXES = frozenset({".md", ".json", ".py", ".js", ".ts", ".sh", ".txt", ".yaml", ".yml"})


def estimate_tokens(text: str) -> int:
    """Rough token count for *text* — see module docstring for the formula."""
    if not text:
        return 0
    thai_chars = len(_THAI.findall(text))
    other_chars = len(text) - thai_chars
    return round(thai_chars * 0.9 + other_chars / 3.8)


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
