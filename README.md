<div align="center">

# 🛩️ agent-takkub

**A desktop cockpit for running a whole team of Claude Code agents — together, in one window.**

You direct a **Lead**; the Lead orchestrates specialist teammates
(frontend · backend · qa · reviewer · devops · …) as live panes.

[![npm](https://img.shields.io/npm/v/agent-takkub)](https://www.npmjs.com/package/agent-takkub)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-blue)

`runs entirely on your machine`

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
- **🌿 Worktree isolation** — parallel agents each work on their own git branch + worktree; you merge when ready, never a commit race.
- **👥 Multi mode** — one toggle turns “1 agent per role” into a fleet (`frontend#1…#K`) sized to your machine.
- **🖥️ Live, steerable panes** — every teammate is a real `claude` process you can watch and interrupt.
- **🗂️ Multi-project tabs** — one isolated Lead per project, no cross-talk.
- **🧩 Batteries included** — Playwright browser automation, curated skill plugins, session memory, decision logs, a self-diagnosing Doctor.
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
| `takkub assign --role frontend --isolation worktree "…"` | task on an isolated git branch |
| `takkub assign --role qa --plan --shards 4 "…"` | plan-first parallel browser QA |
| `takkub worktree list / merge / clean` | review + merge isolated branches |
| `takkub send --to qa "…"` | message a peer (Lead CC’d) |
| `takkub restart` | restart the whole cockpit from the terminal |
| `takkub doctor` | diagnose the environment (`--fix` auto-repairs) |
| `takkub provision` | install / repair plugins + browser tools |

---

## More

- **Architecture & flow diagrams** — [`docs/system-overview/`](docs/system-overview/)
- **How it’s built** — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **From source / one-shot installer** (Chrome, gh, codex, rtk, …) — [`docs/INSTALL.md`](docs/INSTALL.md)

<div align="center"><sub>Windows + macOS · built on PyQt6 · powered by the Claude Code CLI · MIT</sub></div>
