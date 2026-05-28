# Token Reduction Review (2026-05-28)

## 1. Top 3 quick wins (High impact, low risk)
**1. Remove Layout ASCII Art & Trim Project CLAUDE.md**
- **Impact:** ~1-2k tokens saved.
- **Action:** Delete the massive layout ASCII art at the bottom of C:\Users\monch\WebstormProjects\agent-takkub\CLAUDE.md. It provides no actionable logic for the agent. The lengthy routing examples can also be condensed.

**2. Uninstall/Disable agent-skills and superpowers plugins**
- **Impact:** ~4-5k tokens saved.
- **Action:** Since agent-takkub relies on its own panes for specialized roles (e.g., takkub assign --role reviewer), the custom agents from agent-skills (like code-reviewer, test-engineer) and the 39+ combined skills are never used by Lead. Run claude plugin uninstall agent-skills superpowers globally.

**3. Condense Global RTK Instructions (~/.claude/CLAUDE.md)**
- **Impact:** ~1.5k tokens saved.
- **Action:** The RTK guide lists every single command and its token savings table. Replace this with a single strict rule: *Always prefix commands with rtk. Even in command chains with &&, use rtk (e.g., rtk git add . && rtk git commit)*.

## 2. Medium effort
**1. Fix SessionStart Cross-Project Memory Hook**
- **Impact:** Variable (could be 1k-5k+ tokens).
- **Action:** In global ~/.claude/settings.json, a SessionStart hook runs for f in ~/.claude/projects/*/memory/MEMORY.md; do cat ; done. This leaks the memory of *all* projects into every Lead session. Change it to only load the current active project\'s memory, or rely on explicit retrieval.

**2. Scope User-Level MCPs (obsidian-vault & postgres-pms)**
- **Impact:** ~2.5k+ tokens saved.
- **Action:** The 15 tools from obsidian-vault and the postgres-pms tool leak into the Lead pane via global config (as noted in strict-mcp-config-doesnt-block-user-mcps.md). Remove them from the global ~/.claude.json and instead add them only to the .claude/settings.json of the specific projects that need them.

**3. Clean Up Duplicate Memory Entries**
- **Impact:** ~500-1k tokens saved.
- **Action:** Entries like feedback-codex-visible-pane.md have already been fully integrated into the project CLAUDE.md (e.g., the Auto-fire exceptions section). Delete the duplicate .md files from the auto-memory directory so they aren\'t injected twice.

## 3. Trade-offs
- **Removing global obsidian-vault:** Saves 2.5k tokens, but Lead won\'t be able to auto-save notes via the Stop hook unless the MCP is specifically added to the project\'s local config.
- **Trimming RTK instructions:** Removing the explicit list of RTK-supported commands means the model relies entirely on the general always prefix rule. It might occasionally miss obscure commands, but the core ones (git, tsc, cargo) will still work fine.

## 4. No-touch
- **routing_planner.py & orchestrator.py rules:** These encode the source of truth for routing. Leave them as they are; they don\'t consume context unless read, but they allow us to safely trim CLAUDE.md.
- **PreToolUse Hooks (cam-worker-guard & rtk hook):** These are essential for security and filtering and only consume tokens when they output errors.
- **Auto-saving Stop Hook:** The prompt is long, but it only fires at the *end* of the session (Stop), meaning it does not bloat the active context window during the conversation.

## 5. Estimated total saving
- **Doing only Quick Wins:** ~6.5k - 8.5k tokens saved (~10-14%).
- **Doing All (Quick Wins + Medium Effort):** ~10k - 14k tokens saved (~16-23%), plus whatever bloat the cross-project memory hook was pulling in.
