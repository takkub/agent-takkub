# Final Closeout Pack — deliverable (2026-08-24, after 1.3.1/1.4.0)

Per `docs/plans/final-closeout-after-1.3.0/11_MASTER_PROMPT_FOR_LEAD.md` "Final report".

1. **HEAD/version** — `72c9bc4` (1.3.0) at pack intake → **1.4.0** = `8285702` on release.
2. **Delta matrix** — `docs/audit/2026-08-24-final-closeout-phase0.md`. #376 was open (high, not fully closed — case 2/trust-modal + CI regression follow-ups needed); B/C/D/E all MISSING; F/G blocked on user (credentials/eyes); H not exercised.
3. **Changes** — 1.3.1 = `#376` (`e7009e7` marker + `92…` case-2 hold + `52747ec` mock-type guard, release `8d5adc5`). 1.4.0 = B `559a6ec` + C `feedd5a` + D+E `2185a58` + integration fix `ab72801` (test fixture only, doesn't weaken B), release `8285702`.
4. **Isolation evidence** — `docs/audit/2026-08-24-closeout-review.md` §"B — OpenViking strict project scope": layer b (source-level) + layer c (`merge_openviking_traced` re-check) both real and independently tested; missing/`None` project_id fails closed; GLOBAL passes both; A/B cross-project + GLOBAL + missing-metadata tests present and green. **Gap tracked**: `#377` — real sidecar `uri` correlation unverified, so `read`/`hybrid` mode is very likely a no-op today (safe direction: fail-closed, not a leak).
5. **Token/context evidence** — reviewer traced (not just tested) that small tasks skip OpenViking/Resource entirely (call-spy assertion), `TAKKUB_CONTEXT_GATE=0` reproduces the exact pre-gate byte output, budget clamp only ever narrows. One LOW gap: the `flags={"context":...}` override has no real caller yet (`orchestrator._inject_v2_context` never passes it) — dead escape hatch, safe default.
6. **Real GUI evidence** — **NOT DONE**, blocked on user (`27_MANUAL_END_TO_END_SCRIPT.md` items + new Settings pages need eyes).
7. **Real external-service evidence** — **NOT DONE**, blocked on user (21st/Figma/Penpot tokens, live OpenViking instance — see `#377`).
8. **Rollback result** — **NOT EXERCISED** this pass (dev-instance drill from `07_ROLLBACK_AND_FAILURE_DRILL.md` not run — time-boxed out; core disable-flag paths are unit-tested individually — OpenViking `TAKKUB_OPENVIKING_ENABLED=0`, design integrations off individually — but the end-to-end drill sequence wasn't walked).
9. **CI/soak evidence** — 1.3.1: CI green 6/6 twice (`32690679566` pre-fix red on 3 OS from an unrelated Windows-only test assumption regression, then `32691011144`/final green). 1.4.0: CI green 6/6 (`32694773894`). No new soak run this pass (1.3.0's soak evidence in `docs/audit/2026-08-24-master-upgrade-qa.md` still stands for the WebView lifecycle code, untouched by this pack).
10. **#362/Phase 10** — **not touched**, confirmed by reviewer diff grep both passes.

## Stop rule (per `09_RELEASE_AND_STOP_RULE.md`)

Both patch (#376 → 1.3.1) and minor (B/C/D/E → 1.4.0) closeout releases are shipped. Per the pack's own instruction: **stop adding major architecture to 1.x** — remaining open items are field-validation work (F/G/H above) and the `#377` follow-up, not new design. Next work should come from real usage (bug reports, token-usage data, provider failure rates), not another upgrade pack, until #362's own prerequisites are separately satisfied.
