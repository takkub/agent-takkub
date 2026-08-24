# Closeout batch review (2026-08-24)

Reviewer, read-only. Range: `git diff 8d5adc5..HEAD` (5 commits, HEAD=`ab72801`) — B (OpenViking strict scope), C (context/token gate), D+E (Settings UI: Knowledge/OpenViking/Design Tools/Context Debug), plus the `ab72801` integration fix.

Checked against `docs/plans/final-closeout-after-1.3.0/{02_OPENVIKING_STRICT_SCOPE,03_CONTEXT_TOKEN_EFFICIENCY,04_SETTINGS_UI_FINAL,08_OBSERVABILITY_FINAL,11_MASTER_PROMPT_FOR_LEAD}.md`.

**Verification run this session:** `takkub qa-gate --targeted` on all 7 touched test files — PASS (176.6s, all green). `lint-imports` — 29/29 contracts KEPT. Hard constraints (core/ layering, #362 untouched, Brain/Conversation/Graft untouched, no AGPL vendoring, no local LLM) — hold; nothing in this diff touches `core_v2_settings.py`/V2 authority files or vendors OpenViking source.

## Findings (most severe first)

### 1. [HIGH] OpenViking hybrid/read retrieval likely returns nothing against the real sidecar — registry lookup keys don't match what the API is documented to return

**File:** `src/agent_takkub/core/context_sources/openviking_source.py:39-48`, `src/agent_takkub/core/context_sources/indexing.py:176,179,189`

**The claim vs. the code:** `openviking_source.py`'s own docstring says a search hit is looked up "in `indexing.py`'s local registry ... keyed by the exact `path` string handed to the sidecar's ingest call." That's exactly what `indexing.index_vault` does — `registry[str(path)] = _resource_metadata(...)`. The lookup at retrieval time is `indexing.resource_metadata_for_uri(hit.uri)`, keyed by whatever `openviking_adapter.search_resources()` returns as `item["uri"]` from the *sidecar's own search response*.

These are two different identifier namespaces unless the real OpenViking sidecar happens to echo back the exact ingest-time path string verbatim as its search `uri`. The module's own docstring — and `14_OPENVIKING_INTEGRATION.md`'s "Do not hard-code mutable `viking://` URI semantics as canonical Takkub IDs" — is explicit that `uri` is *OpenViking's own* identifier, not Takkub's. A `viking://...`-shaped ID (or any sidecar-assigned ID) would never hit the `str(path)`-keyed registry, `resource_metadata_for_uri` returns `None`, `meta.get("project_id")` is `None`, and `apply_scope_and_trust` rejects with "missing project metadata" — for every real hit, unconditionally.

**Failure scenario:** With a real OpenViking sidecar running in `read`/`hybrid` mode, `OpenVikingSource.retrieve()` silently returns `[]` for every query forever — not a bug that shows up as wrong data, but as "OpenViking never contributes anything," indistinguishable from a healthy-but-empty index. Fail-closed (no cross-project leak — this is the *safe* direction of the bug), but it defeats item B's entire practical purpose for `read`/`hybrid` mode; `shadow` mode's trace-only path shares the same lookup, so even the shadow comparison meant to precede a `read` rollout would show only rejects.

**Why this wasn't caught:** every test that exercises this path (`test_openviking_source_project_a_cannot_retrieve_project_b` and its siblings, `test_openviking_source_maps_hits_to_context_items`) either mocks `search_resources` to return `uri=str(doc)` (matching the registry key by construction) or mocks `resource_metadata_for_uri` directly — none of them exercise a `uri` shaped the way the real sidecar is documented to hand them out. This is exactly the gap `docs/audit/2026-08-24-final-closeout-phase0.md` flagged as item F, "real service validation — BLOCKED (needs credentials/server)": nobody has run this against an actual OpenViking instance, so the mismatch has never had a chance to surface.

**Suggested fix:** either (a) confirm from OpenViking's actual API docs/response samples that `uri` for a resource ingested via `path` *is* that same path string (then a comment recording that plus one test using a non-doctored uri would close the gap), or (b) don't correlate by trusting `uri` shape at all — have `add_resource` capture whatever identifier the ingest response itself returns (if any) and key the registry by that, or fall back to a content/path-hash the search response is more likely to echo. Either way this needs one real round-trip against a live sidecar before `read`/`hybrid` mode ships, not just `shadow`.

---

### 2. [LOW] Context Gate's `flags={"context": ...}` override is unreachable from the real assign path

**File:** `src/agent_takkub/orchestrator.py:979-985` vs. `src/agent_takkub/core/brain/facade.py:75-93`

`build_context_for_assign` accepts an optional `flags: Mapping[str, object] | None` that lets a caller force `task_size` via `{"context": "small"|"medium"|"large"}` (`context_gate._explicit_override`), and `03_CONTEXT_TOKEN_EFFICIENCY.md`/the facade's own docstring both describe it as "the assign-time `--context` escape hatch." The single real call site, `orchestrator._inject_v2_context`, never passes `flags` — `future.submit(build_context_for_assign, project_ns, base_role_a, task, file_read_supported=supports_file_read)`. Grepping the whole `src/` tree for `build_context_for_assign(` and `flags=` turns up no other caller and no `--context` CLI/assign-flag plumbing.

Not a correctness bug — omitting `flags` just falls through to the heuristic (safe default), and every test exercising the override calls `facade.build_context_for_assign` directly rather than through the orchestrator. But the plan's "escape hatch" is currently dead from a user's perspective: there's no way to actually invoke it in production. Worth a follow-up ticket if the override was meant to ship usable in this pack, or worth deleting/documenting-as-future if it wasn't.

---

### 3. [LOW] `apply_scope_and_trust` has no direct unit test for the workspace-mismatch or invalid-trust branches

**File:** `src/agent_takkub/core/context_sources/base.py:157-206`

`02_OPENVIKING_STRICT_SCOPE.md`'s checklist explicitly lists "reject: other project_id, missing/invalid project metadata" and the function itself gates on `workspace_id != WORKSPACE_ID` and `trust not in _VALID_TRUST`, but there's no `tests/test_core_context_sources_base.py` (or equivalent) calling `apply_scope_and_trust` directly with a mismatched `workspace_id` or an invalid `trust` value — those two branches are only reachable today through `ResourceSource`/`OpenVikingSource`, both of which always stamp `workspace_id=WORKSPACE_ID` and a valid trust value themselves, so the reject branches are currently unexercised by any test. Low risk today (single-tenant, `WORKSPACE_ID` is a hardcoded constant every writer already uses correctly), but the moment a second writer stamps something else, there's nothing pinning the reject behavior. A handful of pure unit tests directly against `apply_scope_and_trust` (workspace mismatch, invalid trust, `project_id=None` for both directions) would close this cheaply since the function has zero I/O.

---

## Item-by-item checklist verification

### B — OpenViking strict project scope
- **Layer b + layer c both apply `apply_scope_and_trust`, confirmed by reading the code**: `openviking_source.py` (layer b) and `resource_source.py` (layer b) each call it inside their own `retrieve()`; `context_builder.merge_openviking_traced` (layer c) calls it again on the merged pool before injection, independent of what each source already did. ✅ genuinely two independent layers, not one call wearing two names — verified via `test_merge_layer_c_rejects_wrong_project_and_records_reason`, which replaces a source's whole `retrieve()` and confirms layer c still catches a wrongly-scoped item.
- **Missing/`None` `project_id` fails closed**: yes — `if not pid: scope_rejects += 1` in `apply_scope_and_trust`, before ever reaching the "is it GLOBAL or the allowed project" check. Confirmed by tracing the code (not just the test).
- **`GLOBAL_PROJECT_ID` passes for both projects**: yes — `pid == GLOBAL_PROJECT_ID` short-circuits the allowed-project comparison entirely, so it's independent of `allowed_project_id`'s value (including `None`).
- **`workspace_id` mismatch → reject**: yes in the code (`item.workspace_id != WORKSPACE_ID`), but see Finding 3 — untested directly.
- **A can't retrieve B / B can't retrieve A / GLOBAL visible to both / missing metadata fails closed**: all four present as real tests for both `ResourceSource` and `OpenVikingSource` (`test_resource_source_project_a_cannot_retrieve_project_b`, `..._project_b_cannot_retrieve_project_a`, `..._global_area_visible_to_both_projects`, `..._no_active_project_fails_closed_on_project_scoped_doc`, plus the `openviking_source` equivalents keyed through the real `indexing.index_vault` registry). All pass locally (see verification run above). **But see Finding 1** — the `OpenVikingSource` tests all supply a `uri` that matches the registry key by construction, so they don't prove the real sidecar's response shape correlates the same way.

### C — Context/token gate
- **`classify_task_size` doesn't error on edge cases**: confirmed — empty/whitespace-only text returns `"small"` via an explicit early return before any regex touches it; very long text falls through to the length-based fallback (`len(text) <= _MEDIUM_MAX_LEN`), pure string ops, no way to raise. `test_classify_empty_text_is_small` covers the empty case; there's no explicit "very large string" test but the code path has no failure mode to test for (no regex catastrophic-backtracking risk in these patterns — all bounded alternation, no nested quantifiers).
- **Small task genuinely skips OpenViking/Resource, not just trace-labeled**: confirmed by reading `facade.build_context_for_assign` — `if policy is None or policy.allow_reference_sources:` gates the *call* to `merge_openviking_traced` itself, and `policy_for("small").allow_reference_sources is False`. `test_small_task_never_calls_openviking_or_resource` spies on `OpenVikingSource.retrieve`/`ResourceSource.retrieve` directly (not just checking output text) and asserts zero calls — real skip, not cosmetic.
- **`TAKKUB_CONTEXT_GATE=0` reproduces pre-gate behavior**: traced through the code — with the gate off, `task_size`/`policy` stay `None`, `budget = base_budget` (unclamped), and the `policy is None` branch of the reference-source gate always calls `merge_openviking_traced` (same as before C existed). `test_gate_disabled_budget_matches_legacy_budget_tokens_for` asserts the actual returned string equals calling `context_builder.build_context` directly — a real equivalence check, not just a claim. Passes.
- **Budget clamp doesn't starve medium/large below what they should get**: `gate_budget` only ever clamps *down* (`min(base_budget, ceiling)`), never up — confirmed in code and by `test_gate_budget_never_exceeds_base_budget`. The plan doc itself says the floors are "policy targets, not hard universal limits," so a base budget below a size's floor is expected behavior, not a bug — the code's own docstring on `gate_budget` states this explicitly and matches the plan.

### D+E — Settings UI
- **Secrets never round-trip back into a UI field**: confirmed for Design Tools — the credential `QLineEdit` is write-only (`SecretManager().set_secret(...)`, then `.clear()`); nothing in `_build_design_tools_view`/`_collect_design_tools_status` calls `get_secret` to populate a field. `_test_penpot` does call `get_secret` but only uses the value locally to construct a client and reports connection success/profile name — never echoes the raw token/base_url into the result panel. `test_credential_field_is_masked` + `test_save_credential_writes_through_secret_manager_and_clears_field` back this up.
- **No secret written to `projects.json` or a committed path**: `openviking_settings.py` (`OpenVikingUiConfig`) has no secret fields at all — only mode/strict_project/include_global/result_limit/timeout. Design credentials go exclusively through `SecretManager`, never through `openviking_settings.save()` or `config.load_projects()`. `.gitignore` gained `/openviking/`, which correctly covers `DATA_HOME/openviking/index/{_registry.json,_last_sync.json,<project>.json}` and the trace file — none of that is committable.
- **Network/health/subprocess calls run off the Qt main thread**: confirmed — every one of them (`adapter.health`, `indexing.index_vault`, `_collect_knowledge_status` → `check_graft`/`check_obsidian`, `_collect_design_tools_status`, `_run_design_tools_test`/`_test_penpot`) is wrapped in `_CallableThread(fn, self)` (a real `QThread` subclass, not a comment claiming so) and only the `resultReady` signal touches widgets back on the main thread. Grepped for `QThread`/`_CallableThread` usage directly rather than trusting the docstring.
- **Context Debug UI survives a trace missing `scope_rejects`/`task_size`**: confirmed — every read of those fields in `_reload_kd_context_debug`/`_kd_ctx_report_text` goes through `trace.get(...)` with a `"—"` fallback, never `trace["..."]`. `test_trace_with_forward_compat_fields_renders_them` (per the test file's grep hit) exercises this.

### Integration fix `ab72801`
Read in full. It's exactly what the commit message claims: `_fake_item()` in `test_core_brain_context_gate_facade.py` gained `project_id="proj"`/`workspace_id=WORKSPACE_ID` defaults matching what every call site in that file already passes as the active project, so the fixture stops looking like a scope-less item to B's layer-c re-check. It does not touch `apply_scope_and_trust`, `context_gate.py`, or any production code — the fail-closed semantics from B are untouched; this is purely "give the test fixture the metadata a real item would have." No weakening.

### Hard constraints
- `core/` layering: `lint-imports` run this session — 29/29 contracts KEPT, including `core-is-bottom-layer`, `core-models-pure`, `core-contracts-pure`.
- `#362`/V2 authority: nothing in the diff touches those files; `docs/architecture/depgraph.json` changes are the expected auto-regen from new imports (`context_gate` module, `apply_scope_and_trust` cross-references), not a manual edit.
- Brain/Conversation/Graft: untouched as modules; `facade.py` only adds a `context_gate` import and the new gate branch, doesn't change Brain/Conversation semantics.
- No AGPL vendoring: confirmed — `openviking_source.py`/`openviking_adapter.py` remain HTTP-client-only; nothing under `src/agent_takkub/` pulls in OpenViking source.
- No local LLM: nothing in this diff adds one.

## Ship/no-ship per commit

| Commit | Verdict | Why |
|---|---|---|
| `559a6ec` (B) | **Ship with a follow-up** | Fail-closed logic is real and well-tested at the unit level; Finding 1 means `read`/`hybrid` mode is very likely non-functional against a real sidecar today — safe to ship (defaults to `shadow`, fails closed, no leak) but needs the uri/registry correlation verified against a live OpenViking instance before flipping any deployment to `read`/`hybrid`. |
| `feedd5a` (C) | **Ship** | Every checklist claim traced through the code and passes targeted tests; Finding 2 is a real but low-risk gap (dead escape hatch, safe default). |
| `86efe02`/`411b305` (merges) | **Ship** | Clean merges, depgraph regen only. |
| `ab72801` (integration fix) | **Ship** | Verified it's a test-fixture-only fix, doesn't weaken B. |
| D+E (`settings_knowledge_design.py`, `openviking_settings.py`, `settings_window.py` changes) | **Ship** | Secret masking, thread-off-main-thread, and optional-field UI resilience all verified in code, not just tests. |

**Bottom line:** no CRITICAL or blocking-severity findings. One HIGH finding (#1) is a functional-not-security risk that item F of the master plan (real service validation) was always going to be needed to catch — it's the direct, predictable consequence of that item being blocked, not a new defect introduced carelessly. Recommend tracking it explicitly rather than re-opening B's implementation.
