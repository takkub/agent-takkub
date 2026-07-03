<div align="center">

# 🛩️ agent-takkub

**A desktop cockpit for running a whole team of Claude Code agents — together, in one window.**

You direct a **Lead**; the Lead orchestrates specialist teammates
(frontend · backend · qa · reviewer · devops · …) as live panes.

`Windows` · `macOS` · runs entirely on your machine

</div>

---

## Install

```bash
npm install -g agent-takkub    # isolated Python runtime + a Desktop icon
claude login                   # one-time auth — your Claude account
takkub provision               # recommended plugins + browser tooling
```

Then **double-click “Takkub Cockpit” on your Desktop** — or run `agent-takkub`.

> **Prerequisites:** Node ≥ 18 and Python ≥ 3.11 — *detected, never reinstalled.*
> No clone, no manual wiring. Everything lands in an isolated `~/.agent-takkub`;
> your existing `claude` CLI, plugins, and config are left untouched.

---

## Why

- **🧠 One Lead, many specialists** — talk to the Lead; it spawns the right roles on demand.
- **🔀 Real parallelism** — independent features fan out across panes; QA always runs last.
- **🖥️ Live, steerable panes** — every teammate is a real `claude` process you can watch.
- **🗂️ Multi-project tabs** — one isolated Lead per project, no cross-talk.
- **🧩 Batteries included** — Playwright browser automation, curated skill plugins, session memory, decision logs.
- **🔒 Local-first** — no SaaS, nothing leaves your machine; runs on your logged-in `claude`.

---

## The flow

```
you  →  Lead  →  assign  →  spawn claude pane  →  work  →  done  →  verify (QA)  →  ship
                    ↑__________________ fan out across roles __________________↑
```

Say what you want. The Lead plans it, dispatches the right specialists, gathers
their results, runs verification, and proposes the ship — you stay in control.

---

## Everyday commands

| Command | |
|---|---|
| `takkub assign --role backend "…"` | spawn + task a teammate |
| `takkub send --to qa "…"` | message a peer (Lead CC’d) |
| `takkub provision` | install / repair plugins + browser tools |
| `takkub doctor` | diagnose the environment |

---

## More

- **Architecture & flow diagrams** — [`docs/system-overview/`](docs/system-overview/)
- **From source / one-shot installer** (Chrome, gh, codex, rtk, …) — [`docs/INSTALL.md`](docs/INSTALL.md)

<div align="center"><sub>Windows + macOS · built on PyQt6 · powered by the Claude Code CLI</sub></div>
