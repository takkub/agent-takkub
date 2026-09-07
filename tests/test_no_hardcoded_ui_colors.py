"""Guard (#506): no hardcoded `#rrggbb` color literals in cockpit UI code
outside `cockpit_theme.py` — every color must be a theme token so the
light/dark variant switch reaches it. New hex literal? Add a token to
`cockpit_theme.py` (BOTH `DARK_TOKENS`-backed constant and `LIGHT_TOKENS`
entry) and import that instead — see `capabilities/skills/cockpit-ui-style/`.

Files with a documented reason to keep literals are allowlisted below with
that reason spelled out; extending the allowlist requires the same.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "agent_takkub"

# Quoted hex color strings, plus `color:#rrggbb`-style literals inside
# (f-)strings. Comment issue-refs (#506) never have 6 hex digits, so a plain
# word-boundary hex match inside string/QSS context is precise enough.
_HEX_LITERAL = re.compile(r"[\"'(:=\s]#[0-9a-fA-F]{6}\b")

# path (relative to SRC_ROOT, posix) → why hardcoded hex is allowed there.
_ALLOWLIST: dict[str, str] = {
    "cockpit_theme.py": "the token registry itself — the ONLY home for color values",
    "roles.py": "Role.color identity palette (mirrored by ROLE_COLORS, guarded by "
    "test_role_registry_sync) — role identity is deliberately unthemed",
    "custom_roles.py": "default color assigned to a brand-new custom role (identity data, "
    "not chrome)",
    "issues.py": "GitHub label colors — external data rendered as-is, not cockpit chrome",
    "design_review_html.py": "generates a standalone HTML artifact viewed in a browser, "
    "not a Qt surface; its palette is self-contained",
    "settings_management/pages/roles_page.py": "the role-color input's default TEXT value "
    "(identity data typed into a QLineEdit, not chrome styling)",
    "token_meter.py": "_USAGE_FALLBACK — the Qt-free fallback ramp for usage_color(); even a "
    "lazy cockpit_theme import is a static edge putting PyQt6 under agent_takkub.core "
    "(import-linter core-is-bottom-layer). Sync with the dark USAGE_* tokens is guarded by "
    "test_cockpit_theme.py::TestThemeVariants",
}


def _violations() -> list[str]:
    out: list[str] = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        rel = py.relative_to(SRC_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # pure comment line
            if _HEX_LITERAL.search(line):
                out.append(f"{rel}:{lineno}: {line.strip()}")
    return out


class TestNoHardcodedUiColors:
    def test_no_hex_literals_outside_cockpit_theme(self) -> None:
        violations = _violations()
        assert not violations, (
            "hardcoded #rrggbb literal(s) outside cockpit_theme — add a theme token "
            "(dark + light) instead:\n" + "\n".join(violations)
        )

    def test_allowlist_entries_still_exist(self) -> None:
        """A stale allowlist row silently widens the guard's blind spot."""
        for rel in _ALLOWLIST:
            assert (SRC_ROOT / rel).exists(), f"allowlisted file gone: {rel}"
