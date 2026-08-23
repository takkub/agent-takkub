# Implementation Phases

Do not implement as one giant PR.

## Phase 0 Baseline/Audit
Re-read current main, record HEAD/version, architecture ADR, preserve pane keepalive tests.

## Phase 1 Workspace Shell + Explorer
QSplitter, collapse/restore, lazy tree, external open/copy path. No editing yet.

## Phase 2 Read-only Monaco
Local bundle, QWebChannel, open text files, internal Monaco tabs, terminal path -> Open in Takkub.

## Phase 3 Safe Editing
Ctrl+S, atomic writes, dirty state, disk-change/conflict UI, binary/large-file handling.

## Phase 4 Git Changes + Diff
Changes section, diff editor, refresh/debounce.

## Phase 5 Preview
Per-project URL/file preview, device presets, CLI action, navigation security.

## Phase 6 Design Workflow
Designer role/policy, design publish, auto-focus Preview, Approve/Revise, reuse `design_review_html.py`.

## Phase 7 Optional Design MCP
Storybook first when available; reference/Figma/Penpot optional via Capability Hub.

## Phase 8 Obsidian Hardening
Canonical IDs, persistent dedup, curated boundary.

## Phase 9 OpenViking Adapter (optional/separate follow-up)
HTTP/MCP, health, read/index first, no operational-memory takeover.

## Phase 10 Polish/Diagnostics/Docs/Soak
Editor/Preview/Windows WebEngine long-run checks.

Core Workspace must ship without OpenViking dependency.
