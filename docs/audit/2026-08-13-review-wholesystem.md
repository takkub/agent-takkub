# Whole-System Audit & Review (2026-08-13)

This is a comprehensive review of the `agent-takkub` codebase, comparing the actual code against architectural documentation, project guidelines, and provider capabilities.

## 1. `docs/architecture/godfile-map.md` vs. Code Reality

The `godfile-map.md` document is largely accurate to its July 2026 snapshot, though it highlights architectural debt that remains unresolved.

*   **Verified Extracted Modules**: The modules `pipeline_executor.py`, `orchestrator_text.py`, `lead_inbox.py`, and `spawn_engine.py` exist and contain the methods attributed to them (e.g., `_scan_done_evidence` and `set_session_goal` are correctly located in `src/agent_takkub/orchestrator.py` as noted in `godfile-map.md:99`).
*   **Verified Removals**: The map notes that `_maybe_fire_remote_bridge` and `_on_ui_review_clicked` were removed (`godfile-map.md:148, 169`). A codebase-wide grep confirms these no longer exist.
*   **The God-File Reality**: `src/agent_takkub/orchestrator.py` remains a massive monolith (~4,045 LOC) because the `command_surface`, `session_persistence`, and `watchdogs` clusters were never extracted (`godfile-map.md:28-31`).
*   **Missing Isolation**: `spawn_engine.py` is documented as the "gravitational center" but is implemented as a mixin that shares state dictionaries (`_spawn_queue`, `_spawn_deferred`) across the `Orchestrator` class (`godfile-map.md:89`). This is a hidden cross-mixin coupling rather than a true isolated state object.

## 2. `CLAUDE.md` + `docs/lead/*.md` vs. Code Enforcement

Several strict rules defined in the prompt guidelines are NOT enforced by the actual code, relying entirely on agent compliance.

*   **Lead Direct-Edit Policy**:
    *   *Rule*: `CLAUDE.md:5` and `CLAUDE.md:143` strictly forbid the `lead` role from modifying project source code, requiring tasks to be delegated.
    *   *Reality (Unenforced)*: `src/agent_takkub/pane_guard.py` explicitly exempts the `lead` role from all bash shell restrictions (`pane_guard.py:69` defines `_UNGUARDED_ROLES = frozenset({"lead", "shell"})`). The Lead agent is technically capable of running any destructive or editing command via bash.
*   **Test Execution Limits**:
    *   *Rule*: `CLAUDE.md:24` mandates that intermediate tasks run targeted tests, reserving the full test suite for the QA batch gate to save tokens.
    *   *Reality (Unenforced)*: There is no programmatic guard (e.g., in `pane_guard.py`) to block `pytest` or `npm test` without targeted file paths.
*   **Tool Policy Bypass**:
    *   *Rule / Reality*: As noted in `godfile-map.md:58`, MCP tool restrictions (`pane_tools_policy.py`) do not apply to the bash shell (`pane_guard.py`). Disabling an MCP tool does not prevent a pane from manually executing equivalent commands via `npx` or `curl`.

## 3. ProviderSpec #103 Capabilites Gap (Provider x Feature)

Analysis of `src/agent_takkub/provider_spec.py` reveals significant feature gaps for non-Claude providers.

| Provider | MCP Isolation (`mcp_adapter_variant`) | Reasoning Effort (`effort_flag`) | Task Pointer (`system_prompt_flag`) | JSONL Transcript | Remote History |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **claude** | `strict` (`:293`) | `--effort` (`:311`) | `--append-system-prompt-file` (`:273`) | True (`:319`) | True (`:321`) |
| **codex** | `session_override` (`:370`) | `-c` (`:412`) | None (`:352`) | True (`:422`) | True (`:424`) |
| **gemini** | `plugin_import` (`:469`) | None (`:495`) | None (`:447`) | True (`:496`) | True (`:498`) |
| **opencode** | `none` (`:558`) | None (`:575`) | None (`:535`) | False (`:576`) | False (`:588`) |
| **kimi** | `none` (`:637`) | None (`:653`) | None (`:621`) | False (`:654`) | False (`:659`) |
| **cursor** | `none` (`:708`) | None (`:726`) | None (`:695`) | False (`:728`) | False (`:733`) |

*All references map to line numbers in `src/agent_takkub/provider_spec.py`.*

**Key Gaps Identified**:
1.  **MCP Security Leak**: `opencode`, `kimi`, and `cursor` have `mcp_adapter_variant="none"`, meaning global user MCP configurations can leak into explicit-empty role sessions (`provider_spec.py:558, 637, 708`). `gemini` uses `plugin_import` which also mutates global state (`provider_spec.py:469`).
2.  **Remote Telemetry Blindspots**: `opencode`, `kimi`, and `cursor` do not produce JSONL transcripts, resulting in broken remote history mirroring (`provider_spec.py:588`).

## 4. Systemic Risks

*   **Multiple Sources of Truth for Roles**: According to `godfile-map.md:54`, roles (`critic`, `designer`, `gemini`, `codex`) are hardcoded strings scattered across `roles`, `provider_config`, `routing_planner`, `shared_dev_tools`, and `orchestrator.py`. There is no shared Enum, making it dangerously easy to miss an update when adding or modifying a role.
*   **Host Destructive Commands**: `pane_guard.py:197` implements `_HOST_DESTRUCTIVE_PATTERNS` to block commands like `taskkill /IM node.exe` because agents were killing host-wide processes. This is a fragile regex block that could be bypassed with alternative syntax or shell aliases.
*   **UI Thread Blocking / State Mutability**: Incoming CLI commands are sent via TCP to `cli_server.py`, which mutates state via PyQt signals (e.g., `lead_inbox.py:84` `_pending_lead_cc`). If a large batch of subagents invoke `takkub done` concurrently, the signal flood could cause race conditions in state updates or freeze the main UI thread. (unproven edge case, but structurally risky).

## 5. Bottlenecks for Future Growth

*   **Terminal Scraping Brittle-ness**: The core detection mechanism for whether a provider is "ready" or "busy" relies on regex scraping of terminal output (e.g., matching `"ctrl+p commands"` for OpenCode in `provider_spec.py:544` or `"esc to cancel"`). This makes the system incredibly brittle to minor upstream CLI updates from providers.
*   **`orchestrator.py` God Object**: At over 4,000 lines, this file remains the routing and event bottleneck. Growth into more complex workflows (e.g., graph-based execution rather than parallel/sequential) will be extremely difficult to safely implement within this monolith.
