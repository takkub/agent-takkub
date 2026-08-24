# OpenViking Managed-Local Pack — Phase 0 audit (2026-08-24)

Pack: `docs/plans/openviking-managed-local-2026-08-24/` · HEAD at intake: `d63f87d` (v1.4.0 + final closeout doc)

## Verified against the real upstream (volcengine/OpenViking, AGPL-3.0, ~26.4k★) via WebFetch/WebSearch today — pack's own assumptions hold:

- `pip install openviking --upgrade` installs both library + `openviking-server` CLI — **no Docker required**, confirms `04_NO_DOCKER_INSTALL.md`'s core premise.
- `openviking-server init` → interactive wizard, writes `~/.openviking/ov.conf`. `openviking-server doctor` validates config/Python/provider connectivity/disk without a running server — maps directly onto `07_SETUP_WIZARD.md` + `11_UPDATE_REPAIR.md`'s repair step.
- `openviking-server [--port N] [--config path]` — default port **1933** (matches existing `openviking_adapter.py`'s default). Web Studio bundled at `/studio` since v0.3.21 — confirms `09_WEB_STUDIO.md`.
- `GET /health` → `{"status":"ok"}`; `POST /api/v1/resources` accepts an explicit `to="viking://resources/..."` and returns `result.root_uri` — **this directly explains issue #377** (fixed separately, see that issue): current `indexing.py` never sets `to`, so the registry key (`str(path)`) never matches what `/api/v1/search/find` later returns as `uri`.
- Providers: Volcengine, OpenAI, Codex OAuth, Kimi, GLM, local Ollama (auto-detects/installs runtime + pulls models) — Ollama path means a fully-local zero-external-API-key setup is possible, relevant to `07_SETUP_WIZARD.md`'s provider picker and `13_FRIEND_FLOW.md`'s "no Docker knowledge required" claim.

## Delta matrix

| Item | State | Evidence |
|---|---|---|
| HTTP adapter, health/search/index, fail-open, strict scope | **DONE** (1.3.0/1.4.0) | `core/context_sources/openviking_adapter.py`, `02_OPENVIKING_STRICT_SCOPE.md` closed |
| Managed installer (isolated venv, pip install) | **MISSING** | no `src/agent_takkub/openviking/` module exists |
| Process supervisor (start/stop/health/restart/backoff, ownership tracking) | **MISSING** | nothing spawns `openviking-server`; `remote/tunnel.py` + `remote/http_server.py` are the closest existing pattern to reuse (subprocess lifecycle + owned-vs-external tracking) |
| Setup wizard | **MISSING** | `remote/settings_dialog.py` is the UI pattern to mirror |
| Settings UI managed-runtime controls (status/start/stop/repair/update/remove/Open Studio) | **PARTIAL** | 1.4.0's `settings_knowledge_design.py` OpenViking page has mode/strict/limits/timeout + Test/Sync/Re-index, but assumes an externally-running sidecar — no install/lifecycle controls |
| CLI `takkub ov managed ...` | **MISSING** | only `takkub ov index`/`status` exist (index-time, not lifecycle) |
| Update/repair/remove | **MISSING** | — |
| Open Studio launch | **MISSING** | — |
| Cross-platform (Windows primary + macOS/Linux) | **N/A yet** | no code to test |

## Plan (phases per `16_PHASES.md`, adapted to Lead wave discipline — smaller/serial after the stutter feedback this session)

1. Wave 1 (1 pane): managed installer + venv (`openviking/installer.py`) + process supervisor (`openviking/process.py`, `manager.py`) — reuse `remote/tunnel.py`'s subprocess-ownership pattern, localhost-only, no tunnel/no public bind.
2. Wave 2 (1 pane, after 1 merges — same settings area as 1.4.0's OpenViking page): extend `settings_knowledge_design.py` OpenViking section with managed-runtime controls (Install & Enable, Repair/Update/Remove, Open Studio, Start automatically) + setup wizard dialog.
3. Wave 3 (1 pane): CLI `takkub ov managed status|install|start|stop|restart|doctor|update|repair|remove` + doctor integration.
4. Wave 4: failure/rollback tests (port occupied, broken venv, crash backoff, external-process-never-killed) + cross-platform (Windows done locally; macOS via CI).
5. Reviewer + QA pass, release (minor — new user-visible capability).

Constraints carried forward: localhost-only (no tunnel/0.0.0.0), never kill an externally-owned process, `core/` stays UI-free, doesn't touch #362, no auto-install on boot (explicit user action only), health/start never on Qt main thread.
