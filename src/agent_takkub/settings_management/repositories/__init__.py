"""Repository layer — one adapter per entity, all sharing the contract in
``docs/design/2026-07-11-settings-redesign-codex.md`` §Repository contract:
``list / get / capabilities / create / update / delete_plan / delete``.

Pages never import a JSON path or a data-layer module directly — they call
through a repository so the on-disk shape can change without touching UI.
"""

from __future__ import annotations
