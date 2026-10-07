# Busy-input behaviour per provider: steer vs queue (2026-10-07)

Backlog 7962a2e9 (follow-up to #809, replaces the full delivery-ledger card 0be05632).
Question: when cockpit types a message and presses **Enter while the provider CLI is
mid-turn**, does the message steer into the running turn, wait in a queue until the
turn ends, or sit in the composer as an unsent draft?

## Method

`tools/steer_probe.py` runs each CLI in a real ConPTY (Windows 11, 160x50) in a fresh
temp directory, using the same autonomy flags cockpit passes. It then:

1. Sends the task "run `sleep 5` four times, one call at a time, print `STEP-n-DONE`
   after each".
2. Once `STEP-1-DONE` is on screen, types a change of plan ("after every remaining
   STEP line, also print PINE+APPLE in uppercase") and presses Enter.
3. Records where `PINEAPPLE` first appears in the output stream relative to
   `STEP-4-DONE`. If it appears before, the message steered. If it appears after,
   the message was queued. If it never appears, the message was lost or left as a
   draft.

The steer text never contains the literal `PINEAPPLE`, so its echo can't produce a
false positive. Raw PTY output was inspected for each run.

## Results

| Provider | Version | Enter mid-turn | Applied | On-screen evidence |
|---|---|---|---|---|
| claude | 2.1.292 | accepted into the queue | **steer**: after the next tool call (`STEP-2-DONE` then `PINEAPPLE`) | `❯ Press up to edit queued messages` |
| gemini (agy) | 1.3.1 | accepted into the queue | **after the turn ends**: the model replied that all 4 steps had already finished | `▸ <message>` + `Press up to edit queued messages` |
| opencode | 1.18.34 | accepted into the queue | **steer**: after the next tool call (`STEP-2-DONE` then `PINEAPPLE`) | message block tagged `QUEUED` |
| codex | 0.160 | Enter = steer, Tab = queue | see #809 (2.2.4) | `Messages to be submitted after next tool call` |
| cursor | — | **GAP**: not verified | — | `cursor-agent` is not installed on the probe machine |

No provider tested left the message as a draft. Enter is enough for claude, agy and
opencode, so `busy_queue_marker` and `busy_queue_key` stay unset for them (see
`provider_spec.py`).

## Compact command per provider (same day, live)

| Provider | Manual compact | Live evidence | Idle watchdog |
|---|---|---|---|
| claude 2.1.292 | `/compact` | real watchdog fired, `compact_boundary` 50,363 → 4,261 tokens | yes |
| codex 0.160 | `/compact` | documented, and prod rollouts record `compacted` items (2026-09-29) | yes. Lead also has the cached-input policy |
| opencode 1.18.35 | `/compact` | real watchdog fired, "Compaction · 5.0s", context 16K → 2.7K | yes (v2.2.8) |
| agy 1.3.1 | **none**: `/compact` and `/compress` return "Unknown command" | changelog describes its own threshold-based compaction | no. agy compacts itself |
| cursor | not verified | CLI not installed | no |

Before v2.2.8 the draft check could not see a typed draft in opencode. Its composer is a
`┃` box with no prompt glyph, and the empty-state placeholder is grey rather than faint.
v2.2.8 reads the visible terminal cursor instead: the cursor sits at bar+3 when the
composer is empty and after the last typed character when it is not. Verified live, so
the watchdog never types `/compact` over a user's draft.

## What this means for cockpit

- A `takkub send` to a busy claude or opencode teammate reaches the running turn at
  the next tool boundary, the same as codex since #809.
- On agy, a correction sent mid-turn is applied only after the current turn finishes.
  The turn keeps running on the old instructions. If the change must take effect now,
  interrupt the turn first (Esc) and then send.
- "Delivered" therefore means "accepted by the CLI", not "applied now". The full
  ledger (0be05632) is not needed for correctness. The remaining risk is agy's late
  apply, which is documented here instead.
- Cursor still needs one run of this probe on a machine with `cursor-agent`:
  `python tools/steer_probe.py cursor out -- cursor-agent --force`.
