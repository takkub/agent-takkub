# Cross-Platform Robustness Audit Results (Mac / Windows)
**Date:** 2026-07-10
**Auditor:** Gemini (second-brain reviewer)
**Scope:** Read and analyze cross-platform robustness of the cockpit codebase (Mac/Windows).

---

## Summary of Findings
A thorough sweep of the repository has revealed several critical cross-platform gaps, packaging inconsistencies, and tool-resolution fragilities that affect either macOS, Windows, or both. These issues range from false-positive warnings in diagnostic tools to complete execution failure under specific setups.

---

## Detailed Audit Findings

| file:line | severity | อธิบาย | fix |
| :--- | :--- | :--- | :--- |
| [doctor.py:L140-166](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/doctor.py#L140-166) | `breaks-mac` | **False-Positive Authentication Warning on macOS**: macOS Claude Code stores login credentials in the system Keychain under the service name `"Claude Code-credentials"`, not in a file. However, `doctor.py` only probes `~/.claude/credentials.json`. Because this file is absent on macOS, it raises a false-positive `WARN: credentials.json not found` and prompts the user to log in again even if they are already logged in and the cockpit works perfectly. | Probe the macOS Keychain using `security find-generic-password -s "Claude Code-credentials" -w` (similar to `limit_status.py`) before reporting a warning on macOS. |
| [doctor.py:L112](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/doctor.py#L112), [doctor.py:L140](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/doctor.py#L140) | `breaks-both` | **Typo in Credentials File Name**: The actual file created by `claude login` on Windows/Linux is named `.credentials.json` (with a leading dot). However, `doctor.py` checks for `credentials.json` (no dot) in both Windows (L112) and POSIX (L140) code blocks, causing the check to skip authentication status on Windows and report a false warning on Linux. | Correct the typo by changing `credentials.json` to `.credentials.json`. |
| [lead_context.py:L640](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_context.py#L640), [pane_tools_dialog.py:L56](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L56), [plugin_installer.py:L125](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/plugin_installer.py#L125) | `breaks-both` / `risky` | **Hardcoded ~/.claude Configuration Path for Plugins**: These files hardcode the plugins directory path to `~/.claude/plugins/`. In installed package mode, the default Claude configuration directory is isolated at `DATA_HOME / "claude-config"`. If user profiles are used, the path is `CLAUDE_CONFIG_DIR`. Hardcoding `~/.claude` causes a mismatch between the GUI's plugin checks/settings and the actual profile context that the spawned panes run with, breaking environment isolation. | Replace hardcoded references to `~/.claude` with `config.default_claude_config_dir()` or resolve the directory from the active profile's `CLAUDE_CONFIG_DIR`. |
| [plugin_installer.py:L98-110](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/plugin_installer.py#L98-110) | `breaks-both` / `risky` | **Missing Profile/Env Propagation in Plugin Installer**: The installer spawns the `claude` subprocess without propagating the active `CLAUDE_CONFIG_DIR`. Consequently, plugins installed via the GUI will always land in the global `~/.claude` directory rather than the active profile or the isolated package directory (`DATA_HOME / "claude-config"`). | Propagate the active `CLAUDE_CONFIG_DIR` into the environment of the spawned `claude` installer process in `_claude()`. |
| [plugin_installer.py:L102](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/plugin_installer.py#L102) | `breaks-windows` / `risky` | **Bare command subprocess call fails on Windows**: When the node global wrapper is `claude.cmd` rather than `claude.exe`, calling `subprocess.run(["claude", ...], shell=False)` on Windows will raise `FileNotFoundError` because Windows `CreateProcess` does not resolve `.cmd` / `.bat` extensions automatically without `shell=True`. | Resolve the absolute path of the executable using `config.find_claude_executable()` and use it directly instead of the bare `"claude"` command string. |

---
