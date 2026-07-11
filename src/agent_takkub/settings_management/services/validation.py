"""Shared field validation — thin re-exports over the data layer's own
validators so pages never hand-roll a second copy of the charset/collision
rules."""

from __future__ import annotations

import re

from ... import custom_roles

validate_role_name = custom_roles.validate_role_name

# Mirrors custom_roles._COLOR_RE exactly (#rrggbb) — kept as its own literal
# rather than reaching into that module's private name.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_color(color: str) -> bool:
    return isinstance(color, str) and bool(_COLOR_RE.match(color))
