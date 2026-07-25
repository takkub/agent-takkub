# Backend Code Review: agent-takkub

## Overview
This document summarizes the findings from the review of `app.py`, `cli.py`, and `orchestrator.py` in the `agent-takkub` project. These three files form the core backend infrastructure for the agent cockpit, responsible for UI initialization, command-line operations, and agent pane lifecycle management.

## 1. `app.py`
**Role:** The GUI Entry Point and Global Watchdog
- **Initialization:** Sets up the PyQt UI, initializes logging (`setup_logging`), and starts the orchestrator server.
- **Exception Handling:** Uses a custom `sys.excepthook` to gracefully handle unhandled exceptions. This prevents silent crashes on Windows, presenting users with a critical error dialog before terminating.
- **Watchdog:** Implements a "deadman watchdog" (`_start_watchdog` and `WatchdogWorker`) in a background thread to detect if the main Qt event loop hangs, periodically checking a timestamp updated by a `QTimer`.
- **Singleton Locking:** Uses a local TCP port to ensure only one instance of the application runs at a time.
- **Teardown Sequence:** The application manages teardown carefully using `aboutToQuit` and `atexit` handlers to safely stop the orchestrator and cleanly exit threads, avoiding "QThread: Destroyed while thread is still running" errors.

## 2. `cli.py`
**Role:** Command-Line Interface and Request Dispatching
- **Client-Server Communication:** Sends commands to the `app.py` process via a local TCP socket.
- **Role-based Access Control (RBAC):** Strict separation between `LEAD_ONLY_COMMANDS` and `TEAMMATE_ONLY_COMMANDS`. This prevents teammate agents from using management commands (e.g., `spawn`, `close`, `reset`), enforcing the orchestration hierarchy.
- **Command Handling:** Parses arguments using `argparse` and implements various subcommands:
  - Agent Lifecycle: `assign`, `spawn`, `close`, `resume`, `clear`.
  - Introspection/State: `status`, `task show`, `search`.
  - Issue Tracking: Integrates issue management directly (`issue new`, `list`, `close`, `show`).
  - Provider Configs & Env checks: `provider`, `doctor`, `mcp`, `plugins`.

## 3. `orchestrator.py`
**Role:** Central State Orchestration
- **Pane Registry & Lifecycle:** Manages all AgentPanes globally and per-project namespace (`_panes_by_project`). Handles `spawn`, `close`, and auto-recovery of stuck panes.
- **Task Dispatching:** The `assign` method handles task distribution. It implements flags like `requires_commit`, `auto_chain`, `plan_fanout`, and features to track task assignment metadata.
- **Done Flow (`done` method):** Manages the critical task completion protocol.
  - Automatically captures "screenshot evidence" from the artifacts directory if relevant to the agent's role (e.g., UI or QA).
  - Condenses long notes and creates permanent markdown session logs (`_save_decision_note`).
  - Implements fan-out and auto-chain progression mechanisms.
- **Watchdogs & Recovery:** 
  - `_check_idle_teammates`: Periodically reminds idle teammates to run `takkub done`.
  - `_check_stuck_panes`: Detects when an agent is stuck (e.g., no meaningful PTY output) and performs automatic recovery (close and respawn).
  - Handles shell UI blocking events (like Windows "Open With" dialogs) to avoid silent failures.
- **Cross-tab Communication:** Emits Qt signals (e.g., `crossTabDone`) to ensure the user interface stays responsive and synchronized across different project tabs.
- **Asynchronous Checks:** Offloads blocking operations, like checking `git status` (`_check_uncommitted_async`), to background processes (`QProcess`) to prevent stalling the Qt main thread.

## Summary
The backend architecture is highly robust, employing advanced fail-safes such as the deadman watchdog, stuck pane recovery, and asynchronous event processing. The strict RBAC enforcement in `cli.py` ensures agents adhere to their roles, maintaining the hierarchical structure led by the "Lead" agent. The orchestrator cleanly abstracts agent complexity, tracking task assignments, handling error states, and managing evidence collection and session logging reliably.
