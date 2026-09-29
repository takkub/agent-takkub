# Fix-loop ceiling (#762)

Code: `src/agent_takkub/fix_loop.py` + `Orchestrator._fix_loop_hook` / `_build_verify_fail_handoff`.

- **Key** = project + work identity (backlog card > issue ref in Lead's spec > hash of Lead's task text) + failure signature (salient-token similarity ≥ 0.5). Never role/provider/raw note, so it survives role/provider switch, pane restart, verify hop.
- **Counted:** every `done --fail`, reviewer/QA fail. One assignment or one QA shard fan-out = 1 attempt. **Not counted:** `--blocked`, precondition mismatch, delivery-pointer failure.
- **tiny/normal:** fail 1 → `รอบที่ 1/2`, fail 2 → `2/2` (still propose-then-fire — the system never auto-dispatches a fix), fail 3 → notice becomes `🛑 FIX-LOOP CEILING` with evidence + prior attempts + new diagnostic options. Stop, tell the user, do not assign another fix for that failure.
- **deep:** always propose + confirm; attempts are listed for information.
- **Reset:** a clean `done` from qa/reviewer/critic/tester/security closes only that work item's sequences.
- **Brief:** `takkub assign --backlog <id>` (or a spec carrying the same issue ref) gets the attempt ledger appended to the task brief, any provider. Storage: `RUNTIME_DIR/fix_loop/<project>/sequences.json`.
