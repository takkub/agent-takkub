---
description: Do not use invoke_subagent, use takkub assign
applyTo: "**"
alwaysApply: true
---

# Native subagent policy

You are operating within the `agent-takkub` codebase, which has its own native agent orchestration system (the Takkub Cockpit).

**CRITICAL CONSTRAINT:**
- Do not use Gemini native subagent tools on your own.
- They are allowed only when Lead selected `takkub assign --mode subagent` for the current task.
- Otherwise delegate through `takkub assign --role <role> "<task>"` via `run_command`.
- If you need to communicate with another agent, use the shell command `takkub send --to <role> "<message>"`.

This is a hard constraint requested by the user after a previous agent broke character and used the wrong subagent system. Do not make this mistake again.
