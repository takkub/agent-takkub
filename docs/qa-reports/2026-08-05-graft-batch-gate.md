# QA batch gate — graft MCP pilot batch (5 works)

**Date:** 2026-08-05
**Scope:** 5 uncommitted works — (1) role_memory.py + orchestrator.py auto-capture failure on FAILED report, (2) shared_dev_tools.py + mcps.py + .claude/agents/*.md graft wired as shared MCP, (3) mcp_bridge.py codex deny-by-default leak fix (regression from work 2), (4) mcp_bridge.py + spawn_engine.py fail-closed when codex MCP resolution fails, (5) doctor.py check_graft
**Verdict:** ⚠ CONDITIONAL PASS — 1 pre-existing failure (not this batch), 1 new formatting issue in this batch, smoke test could not be run at full fidelity (role permission gap, see below)

## 1. Full suite

| Check | Result |
|---|---|
| `.venv/Scripts/python.exe -m pytest tests/ -q` | **1 failed**, rest passed — `tests/test_bm25_search.py::TestSearchRanking::test_more_relevant_doc_ranks_first_english` (`assert 3 == 2`, real filesystem role-memory files leaking into the tmp-home search index despite `monkeypatch.setattr(pathlib.Path, "home", ...)`) |
| Pre-existing check | `git stash -u` (all 22 changed/untracked files) → re-ran the same test in isolation → **same failure**. `git stash pop` restored the batch cleanly (verified `git status`/`git diff --stat` match pre-stash). **Not caused by this batch.** |
| `ruff check src/ tests/` | All checks passed! |
| `ruff format --check src/ tests/` | **2 files would be reformatted**, both touched by this batch: `tests/test_done_evidence.py`, `tests/test_mcp_resolution_fail_closed.py` |
| `.venv/Scripts/lint-imports.exe` | Analyzed 136 files, 506 dependencies — **23/23 contracts kept, 0 broken** |
| `takkub docs-verify` | `0 broken ref(s) found` — ok |

## 2. Smoke test — MCP wiring (works 2–4)

Task asked to "spawn a real pane and confirm it comes up normally, no hang" since MCP wiring changed. **As `qa` role I do not have `takkub assign` permission** (`error: only lead can run 'takkub assign'. you are 'qa'.`) — this is enforced by the CLI role gate itself, so I could not spawn a live pane to fully exercise the new codex MCP resolution path end-to-end.

Substituted with the closest available live check: `takkub doctor --live`, which exercises MCP config resolution and provider markers without a full pane spawn:

```
[mcps]
  ✓ shared-mcp.json     5 server(s)
  ✓ playwright/chrome-devtools/context7/notebooklm/graft   npx ok (connection skipped)
[providers]  codex/gemini/opencode/kimi all marker-verified
[spawn-queue] ✓ wedge   queue empty, arbiter idle
Summary: 30 ok, 1 skip, 6 info
```

`graft` shows up correctly as the 5th shared MCP server, consistent with work 2. This confirms config wiring is intact but is **not equivalent to a real spawn** — it does not exercise `spawn_engine.py`'s fail-closed path or `mcp_bridge.py`'s codex resolution live, since `--live` skips actual MCP connection ("connection skipped"). The `tests/test_mcp_resolution_fail_closed.py` and `tests/test_graft_mcp.py` unit tests for those code paths did pass in the full suite run above, which is the strongest coverage available to me for works 3–4 without Lead spawning a pane.

**Recommend Lead spawn one live pane (any role) directly to close this gap** before merge, since this batch specifically touches spawn-time MCP resolution.

## 3. Conclusion

- Tests/lint/import-contracts/docs: green except 1 **pre-existing, unrelated** bm25-search test failure (confirmed via stash) and 1 **new** ruff-format issue in 2 test files from this batch (cosmetic, `ruff format` would auto-fix).
- Smoke coverage for the MCP-wiring works (2–4) is **partial** — role permission blocked the requested live pane spawn; substituted with `doctor --live` + the batch's own passing unit tests for the fail-closed/graft-resolution paths.
- No regressions attributable to this batch found in the full suite.

**Not fixed by QA per role scope** — reporting to Lead for triage/fix-loop decision.
