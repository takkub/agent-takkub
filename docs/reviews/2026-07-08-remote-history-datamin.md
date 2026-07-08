# Remote history fix — data-min relaxation review (2026-07-08)

**Reviewer:** reviewer (code review — quality/security)
**Scope:** uncommitted diff, `src/agent_takkub/remote/{notify,api}.py` + `static/app.js`
**Focus:** the deliberate data-min relaxation — adding user-typed turns (`kind:"me"`) into `/api/lead/history`.

**Round 1 (initial):** ❌ FAIL — 1 HIGH (blocking).
**Round 2 (re-verify, 2026-07-08):** ✅ **PASS — HIGH blocker closed. Ship-ready.**

---

## Context

Prior audit (`docs/reviews/2026-07-07-remote-security-audit.md`) closed the history
endpoint as *"assistant text only, no user text"* precisely because user turns +
system-injected content can leak workstation detail. This round intentionally relaxes
that to fix the missing user-bubble bug. The task flagged the extraction filter as
**the main risk** — and it is: the relaxation is under-filtered.

---

## H1 — HIGH (blocking): `_lead_user_text` leaks non-human meta records into network history

**File:** `src/agent_takkub/remote/notify.py:153` (`_lead_user_text`), consumed by
`read_recent_lead_messages` (`notify.py:285`).

`_lead_user_text` accepts **any** `type=="user"` record whose `content` is a string
or contains a `text` block. That is *not* the same as "a human typed it". Claude Code
writes many non-human records as `type=="user"` with exactly that shape, and the Lead
pane is a normal `claude` process that produces all of them.

### Proven, not guessed
Scanned the real `~/.claude/projects` store the endpoint reads from (2,399 session
JSONLs). `_lead_user_text` **would extract** all of these as `kind:"me"`:

| Record | `isMeta` | Example (verbatim from store) | Leak |
|---|---|---|---|
| Image placeholder | ✅ | `[Image: source: C:\Users\alice\.claude-work\image-cache\4c39ff23-…\1.png]` | **absolute workstation path** |
| Image dims | ✅ | `[Image: original 1284x2778, displayed at 924x2000…]` | — |
| Resume injection | ✅ | `Continue from where you left off.` | internal control text |
| Skill-injected prompt | ✅ | `Approach this as the design lead at a small studio…` / `Run the "deep-research" workflow…` | skill internals, not conversation |
| Local-command caveat | ✅ | `<local-command-caveat>Caveat: The messages below were generated…` | internal markup |
| Slash-command wrapper | ❌ | `<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args></command-args>` | internal markup |
| Local-command stdout | ❌ | `<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>` | command **output** |

### Why it's reachable on the Lead session specifically
- Lead panes spawn with `--resume` (`spawn_engine.py`) → the `isMeta` `Continue from
  where you left off.` record is **guaranteed** in every resumed Lead session.
- Any image pasted to Lead → absolute `image-cache` path record.
- Any slash command run in Lead (`/graphify`, `/compact`, …) → `<command-name>` +
  `<local-command-stdout>` records.

### Blast radius
`/api/lead/history` is gated by `_check_bearer() + _check_password_gate()` only — **not**
by control mode (`http_server.py:332`). So the leak reaches **view-mode (read-only)**
clients too, not just control clients.

### Root cause
The docstring claims it "Mirrors `chatlog_scanner._user_text_only`" — but (a) that helper
is for *internal* friction-counting, never network exposure, and (b) even it does **not**
filter `isMeta`. Two distinct non-human classes slip through:
1. `isMeta: true` records (image paths, resume, skill prompts, caveats).
2. Non-meta command-wrapper markup: `<command-name>`, `<command-message>`,
   `<command-args>`, `<local-command-stdout>`, `<local-command-caveat>`.

This directly contradicts the data-min guarantee (pulse is count-only, paths were the
audit's L1 concern) — the relaxation reintroduces absolute-path + internal-text leakage.

### Suggested fix (in `_lead_user_text`, before returning)
```python
if rec.get("isMeta"):
    return None
# ... after building `text` / `joined`:
# reject Claude Code local-command / caveat wrapper markup — command
# internals & stdout, not human-typed prose.
if text.startswith(("<command-name>", "<command-message>", "<command-args>",
                    "<local-command-stdout>", "<local-command-caveat>")):
    return None
```
Apply the same guards on the bare-string branch (`notify.py:165-167`) and the
list/`text`-block branch. Add a unit test that feeds an `isMeta` record and a
`<command-name>` string record and asserts `read_recent_lead_messages` drops both.
(Orchestrator-injected task/handoff prompts are legit `type=="user"` string records and
will still surface as `me` bubbles — that's expected, they *were* said to Lead.)

---

## PASS — verified clean

1. **Assistant extraction (`_lead_text_blocks`, notify.py:128)** — unchanged: only
   `type=="text"` blocks in `type=="assistant"` records. `tool_use` / `tool_result` /
   `thinking` skipped. No regression. ✅
2. **`tool_result` on user records** — a `tool_result` block is `type!="text"`, so
   `_lead_user_text` skips it; a string-content tool_result doesn't occur. ✅
3. **Prefix strip (`_strip_remote_prefix`, notify.py:186)** — `text[len(prefix):]` only
   when `startswith` — leading-only, never eats body. ✅
4. **Ordering / interleave (`read_recent_lead_messages`, notify.py:273-289)** — iterates
   JSONL lines in file order, appends in order, `out[-limit:]` preserves order. Assistant
   record short-circuits with `continue` so it never double-counts as user. ✅
5. **Live `working` data-min** — `_lead_activity` (notify.py:109) still emits only the
   coarse category, never tool args. Unchanged. ✅
6. **Contract `{text, kind}`** — `read_recent_lead_messages` → `list[{text,kind}]`;
   `api.lead_history` → `{project, messages}` (api.py); `app.js:844-847` reads
   `data.messages`, renders `kind==="me"` vs `"lead"`. Consistent end-to-end. ✅

---

## Minor / notes (non-blocking)

- **m1 (by-design):** orchestrator-injected Lead task/handoff prompts surface as `me`
  bubbles. Acceptable — they represent what was said to Lead. Left as-is.
- **m2:** leaked `<...>` markup from H1 is rendered via `mdInline` (`app.js:776`). Self-
  origin content so XSS risk is low; the prior audit already tracks the `mdEscape` quote
  gap. Fixing H1 removes this content entirely — no separate action needed.

**Bottom line:** fix H1 (add `isMeta` + command-wrapper filtering to `_lead_user_text`,
with a regression test) before ship. Everything else in the diff is sound.

---

## Round 2 — re-verification (2026-07-08) — ✅ PASS, blocker closed

Backend applied the fix. `_lead_user_text` (`notify.py:165-199`) now:
- rejects `rec.get("isMeta")` **first** (line 177) — before any content-shape branch, so it
  catches isMeta regardless of whether `content` is a string **or** a list;
- rejects the 5 command-wrapper prefixes (`_COMMAND_WRAPPER_PREFIXES`, notify.py:156-162):
  `<command-name>` / `<command-message>` / `<command-args>` / `<local-command-stdout>` /
  `<local-command-caveat>` on **both** the bare-string branch (line 185) and the
  list/`text`-block branch (line 197, after `join`).

### Evidence (proven, not asserted)

**1. HIGH closed — edge classes all drop.** Fed each requested edge through the live function:

| Edge | Result |
|---|---|
| `<command-message>` alone (list block) | `None` ✅ |
| `<command-args>` alone (string) | `None` ✅ |
| image placeholder as **list** content + `isMeta` | `None` ✅ |
| `isMeta` with **list** content (resume) | `None` ✅ |
| `isMeta` with **string** content | `None` ✅ |
| `<local-command-stdout>` list block | `None` ✅ |

**Live store scan** (strongest proof — 1,200 real `~/.claude/projects` session JSONLs,
45,505 `type=="user"` records, 2,282 emitted after filtering):
`{isMeta: 0, cmd_markup: 0, image_path: 0}` — **zero** leaks of any class. Sampled emitted
lines are all genuine human/task prose (Thai user questions, project-creation prompts,
orchestrator-injected `[system]`/`[cockpit restart]` task turns — the accepted by-design m1).

**2. Genuine human turns still pass `kind:"me"`** — desktop string, `[remote → lead]`-prefixed
remote turn, human text containing a literal `<div>`, and multi-line list-block turns all
return their text (not over-rejected). Only the exact 5 command prefixes match, so ordinary
`<`-content is untouched.

**3. Not over-filtered** — 2,282 genuine records survive in the live scan; nothing human is lost.

**4. Prior PASS items still hold** — `_lead_text_blocks` (assistant extraction) unchanged;
ordering/interleave via `out[-limit:]` in file order unchanged; `_strip_remote_prefix`
leading-only; contract `{text,kind}` → `api.lead_history` `{project, messages}` (api.py:150)
→ app.js render, consistent end-to-end.

**Tests:** `test_remote_notify.py` 43 passed; full remote suite (notify+api+http_server) 123
passed. Regression tests cover isMeta (string+list), image placeholder, and command-wrapper
markup dropping while genuine turns survive.

**Note (non-blocking):** a contrived `[remote → lead] <command-name>…` (a human literally
typing command markup into the PWA reply box) surfaces post-strip — but that is self-authored
human input, not Claude Code internal injection, so surfacing it is correct, not a leak.

**Verdict: ✅ PASS — ship-ready.**
