"""Per-provider compatibility ranges — min inclusive / max exclusive (or
inclusive, see `CompatibilityRule.max_exclusive`) + feature flags gated by
version. Built on `core.models.version.CompatibilityRule` rather than a
parallel shape (plan §3.4: reuse the Phase-1 vocabulary, don't redefine it).

Only claude has a real, empirically-set baseline today — it mirrors
`agent_takkub.system_baseline.CORE_TOOLS["claude"]`, the project's one
existing source of truth for "what version is old enough to warn about".
Every other provider is deliberately left UNREGISTERED: doctor.py's own
`check_provider_auth()` docstring says most providers' state is "otherwise a
black box the cockpit deliberately never reads", and the project memory note
on provider rollout explicitly flags opencode/kimi/cursor markers as
"uncalibrated until login" — a guessed min/max there would be a false-
confidence OK or FAIL, worse than no verdict at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_takkub import system_baseline

from ..models.version import CompatibilityRule


class CompatVerdict(StrEnum):
    OK = "ok"
    BELOW_MIN = "below-min"
    ABOVE_MAX = "above-max"
    UNKNOWN = "unknown"  # version string present but unparseable
    UNCALIBRATED = "uncalibrated"  # no rule registered for this provider


@dataclass(frozen=True, slots=True)
class CompatEvaluation:
    provider: str
    verdict: CompatVerdict
    installed: str | None
    rule: CompatibilityRule | None


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Zero-padded tuple compare — same shape as `system_baseline._cmp`,
    kept local since that one is private to its own module."""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _claude_rule() -> CompatibilityRule:
    tool = system_baseline.TOOL_BY_KEY["claude"]
    min_str = ".".join(str(p) for p in tool.minimum)
    return CompatibilityRule(
        id="claude",
        component="claude",
        min_version=min_str,
        max_version=None,
        note=f"mirrors system_baseline.CORE_TOOLS['claude'] (min {min_str})",
    )


class CompatibilityMatrix:
    def __init__(self, rules: dict[str, CompatibilityRule] | None = None) -> None:
        self._rules: dict[str, CompatibilityRule] = (
            dict(rules) if rules is not None else {"claude": _claude_rule()}
        )

    def rule_for(self, provider: str) -> CompatibilityRule | None:
        return self._rules.get(provider)

    def register(self, rule: CompatibilityRule) -> None:
        self._rules[rule.component] = rule

    def evaluate(self, provider: str, version_text: str | None) -> CompatEvaluation:
        rule = self._rules.get(provider)
        if rule is None:
            return CompatEvaluation(provider, CompatVerdict.UNCALIBRATED, version_text, None)
        installed = system_baseline.parse_version(version_text)
        if installed is None:
            return CompatEvaluation(provider, CompatVerdict.UNKNOWN, version_text, rule)
        if rule.min_version:
            min_v = system_baseline.parse_version(rule.min_version)
            if min_v and not system_baseline.meets(installed, min_v):
                return CompatEvaluation(provider, CompatVerdict.BELOW_MIN, version_text, rule)
        if rule.max_version:
            max_v = system_baseline.parse_version(rule.max_version)
            if max_v:
                cmp = _cmp(installed, max_v)
                over = cmp >= 0 if rule.max_exclusive else cmp > 0
                if over:
                    return CompatEvaluation(provider, CompatVerdict.ABOVE_MAX, version_text, rule)
        return CompatEvaluation(provider, CompatVerdict.OK, version_text, rule)

    def supports_feature(self, provider: str, feature: str) -> bool:
        rule = self._rules.get(provider)
        if rule is None:
            return False
        return feature in rule.features


# Process-wide default matrix — providers register into it as their
# baselines get calibrated (mirrors `system_baseline.TOOL_BY_KEY`'s role as
# the single place doctor.py reads from).
DEFAULT_MATRIX = CompatibilityMatrix()
