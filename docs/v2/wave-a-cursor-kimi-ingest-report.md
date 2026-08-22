# 2.0.0 Wave A — cursor + kimi ingest report

> epic #309 · branch `wt/backend-3-1787380953` · 2026-08-22 · plan
> `docs/v2/2.0.0-migration-plan.md` §1.1 ("kimi/cursor conversation ingest")

Closes both halves of §1.1's `core.conversation.ingest` gap. cursor was the
plan's predicted S/low-risk WRAP job; kimi required the investigation the
plan called for (§5 item 3) before any code — full trail below.

---

## 1. cursor ingest — as predicted, a WRAP job

`cursor_adapter.py` wraps `cursor_helper.py`'s existing
`resolve_cursor_jsonl_for_cwd`/`parse_cursor_record_message` directly — no
duplication needed (unlike `codex_adapter.py`, which had to duplicate
`remote/notify.py`'s two-schema parser because that logic lives in the
Qt-importing `remote/` package; `cursor_helper.py`, like `codex_helper.py`,
has no PyQt6 import, so `core.*` may import it under the
`core-is-bottom-layer` import-linter contract). `cursor_store.py` (ingest
cursor persistence) already worked for any provider key — no change there.

Registered in `_ADAPTERS`; `supported_providers()` now includes `"cursor"`.

## 2. kimi ingest — investigated, then built (not closed as unsupported)

### 2.1 What the investigation found

Kimi CLI 1.49.0 is installed on this machine (`~/.local/bin/kimi.exe`,
`uv tool install`). Reading its real `~/.kimi` directory and its own
installed source (`kimi_cli/metadata.py`, `kimi_cli/session.py`,
`kimi_cli/wire/{file,types}.py` under
`%APPDATA%\uv\tools\kimi-cli\Lib\site-packages\kimi_cli`) shows a real,
locally-readable, per-work-dir session store:

```text
~/.kimi/kimi.json                                    work-dir registry:
  {"work_dirs": [{"path": "<canonical cwd, OS-native string>",
                   "kaos": "local", "last_session_id": "<uuid>|null"}]}
~/.kimi/sessions/<md5(work_dir.path)>/<session-id>/wire.jsonl
                                                      per-turn event log
```

`~/.kimi` itself is overridable via `KIMI_SHARE_DIR`
(`kimi_cli/share.py::get_share_dir`) — same env-override shape as
`CURSOR_HOME`/`OPENCODE_HOME` elsewhere in this codebase.

`wire.jsonl` line shape (`kimi_cli/wire/file.py::WireMessageRecord`,
confirmed against a **real recorded line from a genuine prior teammate
task** already on this machine — the actual #256 auth-marker task, not a
synthetic example):

```json
{"timestamp": 1786846569.179342, "message": {"type": "TurnBegin", "payload": {"user_input": [{"type": "text", "text": "[lead → kimi] ..."}]}}}
```

User turns (`TurnBegin`/`SteerInput`) carry `payload.user_input` as either a
plain string or a list of `kosong.message.ContentPart`s. Assistant text
arrives as a top-level `TextPart` message
(`{"type":"TextPart","payload":{"type":"text","text":"..."}}`) —
`kimi_cli/wire/__init__.py::_WireRecorder` subscribes to the **merged**
message queue, so a recorded `TextPart` is already a complete block, never a
streaming delta, which is why the adapter needs no delta-buffering logic.
`ThinkPart` (hidden reasoning) and `ToolCall`/`ToolCallPart` flow through
the same log and are deliberately never surfaced as messages, per the
provider-integration skill's "text only" rule.

This directly contradicts the pre-investigation assumption baked into
`ProviderSpec.kimi_spec` (`produces_jsonl_transcript=False`,
`supports_remote_history=False`) — those flags are correct for their own
consumer (the `remote/notify.py` mobile mirror, tracked separately under
#103) but do not mean "no local store exists". **Left untouched**: this
report does not change `provider_spec.py` or add a `remote/notify.py`
`_HISTORY_SCANNERS["kimi"]` entry — that's explicitly the #103 gap in a
different, still-unwired consumer, out of this task's scope (Lead's
instructions: only build what plan §1.1 asks for).

### 2.2 What was built

`kimi_helper.py` (new, top-level, mirrors `codex_helper.py`/
`cursor_helper.py`'s Qt-free, best-effort, read-only conventions):

- `kimi_share_dir()` / `kimi_metadata_file()` — env-override + default.
- `normalize_kimi_cwd()` — best-effort match to `kaos.path.KaosPath.
  canonical()` (absolute + `.`/`..`-normalized, symlinks **not** resolved,
  case/separators preserved — deliberately *not* the lowercase/posix-folded
  normalization `cursor_helper.normalize_cursor_cwd` uses, because
  `kimi.json` matches on exact string equality against kimi-cli's own
  string, verified against a real `kimi.json` on this machine).
- `resolve_kimi_session_dir(cwd, session_id)` — reads the registry, computes
  the md5-hashed sessions dir, resolves an exact session or the newest one
  with a `wire.jsonl` present.
- `parse_kimi_wire_record()` / `read_recent_kimi_messages()` — the record
  parser described above.

`kimi_adapter.py` (new, `core/conversation/ingest/`) — same full-wrap shape
as `cursor_adapter.py`: imports `kimi_helper.py` directly (no PyQt6 import
in it either), no duplicated parsing logic. Registered in `_ADAPTERS`.

### 2.3 The one honest gap left in this adapter

This machine's kimi CLI has **no default model configured**. Reproduced
live during this investigation (`kimi --print --output-format text --yolo
-p "..."` in a scratch dir → `LLM not set`) — the *exact* same failure
shape as the one real prior session found on disk (`TurnBegin` immediately
followed by `TurnEnd`, nothing recorded between them). So: a real user turn
has been read and parsed from a real store (`test_kimi_adapter_reads_a_real_
local_session_if_present`, `tests/test_core_conversation_ingest.py`), but no
real assistant-authored `TextPart` line has ever been observed on this
machine to verify the assistant branch against — the parser for that branch
is built directly from kimi-cli's own typed Pydantic wire-protocol source
(`kimi_cli/wire/types.py::TextPart`, `.model_dump()` shape pinned by that
class's own doctest) rather than a hand-written fixture, which is the
strongest verification available without a working model, but it is still
not the "verify against a live store" bar the provider-integration skill
asks for on the reply side. **Recommendation**: once a real cockpit
completes a signed-in kimi turn with model output, re-run
`test_kimi_adapter_reads_a_real_local_session_if_present` against that real
`wire.jsonl` and confirm at least one `("lead", ...)` result — if the shape
doesn't match, this is exactly the schema-drift lesson (#206/D3) the skill
warns about, and it will fail loudly (empty parse of an ostensibly non-empty
file) rather than silently.

---

## 3. Files changed

```text
src/agent_takkub/kimi_helper.py                              new
src/agent_takkub/core/conversation/ingest/cursor_adapter.py   new
src/agent_takkub/core/conversation/ingest/kimi_adapter.py     new
src/agent_takkub/core/conversation/ingest/__init__.py         modified — registers both, docstring rewritten
tests/test_kimi_helper.py                                     new — 22 tests
tests/test_core_conversation_ingest.py                        modified — +cursor/+kimi adapter tests, registry
                                                                assertion now covers all 6, real-store smoke
                                                                tests for both
docs/v2/wave-a-cursor-kimi-ingest-report.md                   this file
```

## 4. Verification

`PYTHONPATH=src takkub qa-gate --targeted tests/test_core_conversation_ingest.py tests/test_kimi_helper.py src/agent_takkub/kimi_helper.py src/agent_takkub/core/conversation/ingest/kimi_adapter.py src/agent_takkub/core/conversation/ingest/cursor_adapter.py src/agent_takkub/core/conversation/ingest/__init__.py`
→ **PASS**, 61 passed / 1 skipped (skip = no local cursor/kimi store branch
that doesn't apply on this particular run; both providers' real-store smoke
tests found and read this machine's actual stores during development).

Not run: full suite (root `CLAUDE.md`'s test-tier rule — this change never
touches `spawn_engine.py` or any orchestrator hot path, so targeted-only is
the correct tier; full suite still belongs to the qa batch gate before
merge).

## 5. For epic #309 / Lead

Both §1.1 items are now code-complete, not just investigated. Suggest
closing §1.1 as: cursor ✅ done; kimi ✅ done, with the §2.3 caveat above
tracked as a small follow-up (verify assistant-`TextPart` branch against a
real signed-in session) rather than a blocking gap — the store, the
resolver, and the user-turn branch are all verified against real data on
this machine, only the reply branch is source-verified-but-not-live-verified.
