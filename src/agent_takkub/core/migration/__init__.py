"""Core V2 migration engine: inspect/plan/dry-run/apply/validate/rollback
over `MigrationStep`s (`core.contracts.migration`), backed by a journal +
copy-never-move backups (#309 Phase 4, docs/v2/V2_IMPLEMENTATION_PLAN.md §5).
"""
