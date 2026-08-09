---
description: Do not use invoke_subagent, use takkub assign
applyTo: "**"
alwaysApply: true
---

# 🚫 STRICT RULE: NEVER USE `invoke_subagent` IN THIS PROJECT

You are operating within the `agent-takkub` codebase, which has its own native agent orchestration system (the Takkub Cockpit).

**CRITICAL CONSTRAINT:**
- You MUST NEVER use the Gemini native `invoke_subagent` tool.
- You MUST NEVER use the Gemini native `manage_subagents` tool to spawn or manage subagents.
- If you need to delegate work or spawn another agent, you MUST use the shell command `takkub assign --role <role> "<task>"` via the `run_command` tool.
- If you need to communicate with another agent, use the shell command `takkub send --to <role> "<message>"`.

This is a hard constraint requested by the user after a previous agent broke character and used the wrong subagent system. Do not make this mistake again.
