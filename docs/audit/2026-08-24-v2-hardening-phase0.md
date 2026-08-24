# v2 Hardening Pack (after 1.4.1) — Phase 0 audit (2026-08-24)

Pack: `docs/plans/v2-hardening-2026-08-24/` · baseline SHA in pack (`0999c2e`) confirmed = v1.4.1 release · HEAD at intake: post-OpenViking-Wave-1 merge · open: #362 only

| Pack item | State on main | Evidence |
|---|---|---|
| B Classifier v2 (structural/risk/confidence) | **MISSING** — Stage 1 only | `context_gate.classify_task_size` = regex keyword (EN/TH) + length; `role` accepted but unused; no risk-domain hard override (auth/payment/migration CAN classify small today), no score/confidence/reasons output |
| C Adaptive escalation (mid-task re-classify, incremental enrichment) | **MISSING** | gate decides once at assign; nothing re-evaluates after files/schema signals appear |
| C Dynamic token controller | **PARTIAL** | static per-size ceilings (`gate_budget` clamp) + `budget_tokens_for` (12% window); no relevance-threshold top-K, no rework-history input, "ceiling not quota" holds (never pads) |
| D Resource governor (app-wide) | **PARTIAL** | `core/scheduling/backpressure.py` exists (spawn backpressure) + #364 RAM levers (WebEngine discard) — but no unified pressure policy across indexers/retrieval/preview/git workers, no priority ladder |
| D Central fail-open matrix + circuit breaker | **PARTIAL / MISSING** | fail-open is component-local (each module try/except) and works (chaos-adjacent tests exist per-module); no central policy labels, **no circuit breaker anywhere** (OpenViking down = every assignment still pays timeout in read/hybrid) |
| E Explainable trace (skip reasons, escalation reason) | **PARTIAL** | trace has task_size/tokens/rejects/dedup; no per-source "skipped because …", no score/confidence/escalation fields |
| G Fast/Automatic/Deep UX | **MISSING** | no user-facing strategy switch; internals exposed via env/Settings only |
| H Retrieval prompt-injection defense | **PARTIAL** | external content marked untrusted in design_clients (Provenance) but context injection does NOT wrap retrieved docs in UNTRUSTED REFERENCE framing; no inert-instruction regression test; secrets redaction in traces unverified |
| I Managed local OpenViking | **IN PROGRESS** | Wave 1 merged (installer/process/port/manager); Wave 2 (wizard/settings/boot) running now |
| J Benchmark v1/Auto/Deep + classifier eval + chaos suite | **MISSING** | QA/unit only; no fixed workload suite, no labeled classifier dataset, no chaos harness |
| 15 Agent spawn policy | **PARTIAL** | Lead routing doc governs; nothing programmatic ties complexity→agent count |
| 16 Rework control | **PARTIAL** | `classify_failure` suggests fix-loop role; no repeated-failure/ping-pong counters |
| 17 Cache policy | **PARTIAL** | graft/status caches exist ad-hoc; OpenViking health not cached (see circuit breaker), no central invalidation policy |
| Do-NOT-rebuild list (strict scope, gate, Settings, #377) | **CONFIRMED SHIPPED** | 1.4.0/1.4.1 |

Order (pack priority, adjusted): OpenViking waves 2-3 finish first (in flight) → A/B classifier v2 + risk override → C adaptive+controller → D breakers/fail-open matrix → E trace/UX → H security → J benchmark/chaos → soak. #362 untouched.
