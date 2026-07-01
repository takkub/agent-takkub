"""System-core baseline — the single source of truth for the tool versions
every cockpit machine should have.

The problem it solves: minimum versions used to be hardcoded ad-hoc across
``doctor.py`` (``(3, 11)`` for Python buried in a check, "Node.js 18+" only in a
message string), so there was no one place that said "this is the bar every
machine must clear". When one machine upgrades Python/Node/Claude, there was no
way to tell whether the others had drifted below the shared line.

This module IS that line. Two tiers per tool:

* ``minimum``     — the hard floor. Below it the cockpit is unsupported →
                    ``doctor`` reports FAIL with an upgrade hint.
* ``recommended`` — the version the fleet standardises on. Above ``minimum`` but
                    below this still runs; ``doctor`` reports WARN ("upgrade to
                    keep every machine equal"). This is the *nudge* tier —
                    non-breaking, so bumping it never blocks an older machine.

Bump the numbers HERE and every machine's ``takkub doctor`` re-points at the new
bar automatically — no other file changes. Keep ``minimum`` in sync with
``pyproject.toml``'s ``requires-python`` (Python) and the CLIs we ship against;
raise ``recommended`` when the team upgrades a machine and wants the rest to
match.

Pure-leaf module: no Qt, no subprocess, no imports of sibling cockpit modules.
``doctor`` feeds it the version strings it already collects and maps the result
onto its ``Finding`` type. That keeps this file trivially unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Version = tuple[int, ...]


@dataclass(frozen=True)
class CoreTool:
    """One tool on the system-core baseline.

    ``key`` is the stable id doctor uses to look the tool up; ``label`` is the
    human name. ``minimum``/``recommended`` are version tuples compared
    component-wise (padded with zeros, so ``(18,)`` satisfies ``(18, 0, 0)``).
    """

    key: str
    label: str
    minimum: Version
    recommended: Version
    upgrade_hint: str


# The dev-team core set. Keep `minimum` aligned with what the cockpit actually
# requires to run (pyproject requires-python for Python; the CLIs we target for
# the rest). `recommended` is the fleet-parity nudge — set it to the version a
# well-maintained machine runs today; raising it only WARNs, never blocks.
CORE_TOOLS: tuple[CoreTool, ...] = (
    CoreTool(
        "python",
        "Python",
        minimum=(3, 11),
        recommended=(3, 11),
        upgrade_hint="install Python 3.11+ from python.org (must match pyproject requires-python)",
    ),
    CoreTool(
        "node",
        "Node.js",
        minimum=(18, 0),
        recommended=(20, 0),
        upgrade_hint="install Node.js 20+ from nodejs.org",
    ),
    CoreTool(
        "npx",
        "npx",
        minimum=(8, 0),
        recommended=(9, 0),
        upgrade_hint="comes with Node.js — reinstall/upgrade Node to refresh npx",
    ),
    CoreTool(
        "claude",
        "Claude Code CLI",
        minimum=(2, 0),
        recommended=(2, 1),
        upgrade_hint="update: npm i -g @anthropic-ai/claude-code (or reinstall from claude.ai/code)",
    ),
)

TOOL_BY_KEY: dict[str, CoreTool] = {t.key: t for t in CORE_TOOLS}


# Levels a machine's installed version can land on relative to the baseline.
LEVEL_OK = "ok"  # meets recommended
LEVEL_RECOMMEND = "recommend"  # meets minimum, below recommended → WARN nudge
LEVEL_BELOW_MIN = "below-min"  # below minimum → unsupported → FAIL
LEVEL_UNKNOWN = "unknown"  # version string unparseable → INFO


@dataclass(frozen=True)
class CoreResult:
    """Outcome of comparing one installed version against a :class:`CoreTool`."""

    key: str
    installed: Version | None
    level: str

    @property
    def installed_str(self) -> str:
        return ".".join(str(p) for p in self.installed) if self.installed else "(unknown)"


_VER_RE = re.compile(r"\d+(?:\.\d+)*")


def parse_version(text: str | None) -> Version | None:
    """Extract the first dotted-number run from *text* as a version tuple.

    Handles every shape the cockpit's ``--version`` probes emit:
    ``"Python 3.11.8"`` → ``(3, 11, 8)``, ``"v22.22.1"`` → ``(22, 22, 1)``,
    ``"10.9.4"`` → ``(10, 9, 4)``, ``"2.1.197 (Claude Code)"`` → ``(2, 1, 197)``.
    Returns ``None`` when no numeric version is present.
    """
    if not text:
        return None
    m = _VER_RE.search(text)
    if not m:
        return None
    return tuple(int(p) for p in m.group().split("."))


def _cmp(a: Version, b: Version) -> int:
    """Compare two version tuples, zero-padding to equal length so a short
    ``(18,)`` is treated as ``(18, 0, 0)`` rather than sorting *below* it."""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def meets(installed: Version, required: Version) -> bool:
    """True when *installed* is at least *required* (length-insensitive)."""
    return _cmp(installed, required) >= 0


def evaluate(key: str, version_text: str | None) -> CoreResult:
    """Grade an installed version string against the tool's baseline tiers."""
    tool = TOOL_BY_KEY[key]
    v = parse_version(version_text)
    if v is None:
        return CoreResult(key, None, LEVEL_UNKNOWN)
    if not meets(v, tool.minimum):
        return CoreResult(key, v, LEVEL_BELOW_MIN)
    if not meets(v, tool.recommended):
        return CoreResult(key, v, LEVEL_RECOMMEND)
    return CoreResult(key, v, LEVEL_OK)


def baseline_note(tool: CoreTool) -> str:
    """Short "min X.Y · rec A.B" suffix doctor appends to a finding's detail so
    every machine sees the exact bar it's being measured against."""
    mn = ".".join(str(p) for p in tool.minimum)
    rc = ".".join(str(p) for p in tool.recommended)
    return f"min {mn} · rec {rc}"
