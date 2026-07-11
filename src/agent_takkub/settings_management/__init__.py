"""Settings Management — redesign of the cockpit Settings window.

Phase 0+1: characterization tests + repository/service contracts + reusable
list-detail shell + Roles vertical slice. Lives entirely apart from the
legacy ``settings_window.py`` (never imported by, never imports it) so the
two coexist behind ``TAKKUB_SETTINGS_UI`` (see :mod:`feature_flags`).

See ``docs/design/2026-07-11-settings-redesign-SPEC.md`` for the design
decision this package implements.

**Import constraint:** this package MUST NOT import ``orchestrator``,
``app``, or ``cli`` (enforced by import-linter contract
``settings-management-layer``).
"""

from __future__ import annotations
