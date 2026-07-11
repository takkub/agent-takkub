"""Cross-repository logic: relationship read/write, delete cleanup, validation.

Kept apart from ``repositories/`` because these functions read/write MORE
than one JSON store per call (the thing a single-store repository adapter
must not do on its own — see SPEC.md "Repository layer").
"""

from __future__ import annotations
