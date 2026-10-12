"""OS-level enforcement of a restricted Lead (#842).

When the effective team policy says ``lead_may_implement: false`` the Lead may
read, brief, assign and report but must not change project files. Prompt text
alone never held that line for a Codex Lead (it ran with the sandbox bypassed),
so the boundary is Codex's own OS sandbox: a session-scoped permission profile
that is read-only everywhere except a short list of cockpit-owned roots.

Codex hooks are deliberately NOT the boundary — they fail open on error and the
vendor documents them as a guardrail, not enforcement. See
``docs/design/2026-10-11-842-codex-lead-enforcement.md`` for the threat model.

A Claude Lead's boundary is Claude Code's own permission engine in ``dontAsk``
mode with an allowlist (Claude Code has no OS sandbox on native Windows). See
``docs/design/2026-10-12-842-claude-lead-enforcement.md``.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
import uuid

from ._win_console import SUBPROCESS_NO_WINDOW

PROFILE_NAME = "takkub_lead"

#: Enforcement modes recorded on the Lead readiness state.
MODE_CODEX_SANDBOX = "codex_sandbox"
MODE_CLAUDE_PERMISSIONS = "claude_permissions"
MODE_NONE = "none"

#: (provider, mode) pairs that actually enforce a restricted Lead → gate label.
_ENFORCED = {
    ("codex", MODE_CODEX_SANDBOX): "os_sandbox",
    ("claude", MODE_CLAUDE_PERMISSIONS): "permission_allowlist",
}

#: Providers that can run a restricted Lead at all.
RESTRICTED_LEAD_PROVIDERS = frozenset(provider for provider, _mode in _ENFORCED)

#: Modes that hold a restricted policy, whichever provider recorded them.
ENFORCED_MODES = frozenset(mode for _provider, mode in _ENFORCED)


def lead_is_restricted(project: str | None) -> bool:
    """True when the project's effective policy forbids the Lead to implement.
    Unreadable policy counts as restricted — the safe direction."""
    from . import team_preset

    try:
        return not bool(team_preset.current(project).get("lead_may_implement"))
    except Exception:
        return True


def lead_scratch_dir(project: str) -> pathlib.Path:
    from .config import RUNTIME_DIR
    from .path_safe import safe_segment

    return RUNTIME_DIR / "lead-tmp" / safe_segment(project or "default")


def lead_write_roots(project: str, env: dict, *, sandboxed_cli: bool = True) -> list[str]:
    """Paths a restricted Lead still needs to write: its briefs/evidence, the
    readiness acknowledgement and a scratch/TEMP dir. Directories are created
    so the sandbox can grant them. A development checkout may store its
    cockpit runtime within the repo; only that runtime subtree is exempted.
    The Lead cannot write the audit event log.

    ``sandboxed_cli=False`` (Claude): the ``takkub`` CLI runs outside any
    sandbox and writes the readiness acknowledgement itself, so the model's
    own edit tools get only the briefs/evidence dirs — never the readiness
    state it could otherwise forge."""
    from .config import RUNTIME_DIR, _project_dict

    dirs = [
        env.get("TAKKUB_DOCS_DIR") or "",
        env.get("TAKKUB_ARTIFACTS_DIR") or "",
    ]
    if sandboxed_cli:
        dirs += [str(RUNTIME_DIR / "lead-readiness"), str(lead_scratch_dir(project))]
    roots: list[str] = []
    project_paths = (_project_dict(project).get("paths") or {}).values()
    protected = [pathlib.Path(raw).resolve() for raw in project_paths if raw]
    runtime_root = RUNTIME_DIR.resolve()
    for raw in dirs:
        if not raw:
            continue
        path = pathlib.Path(raw)
        resolved = path.resolve()
        if any(
            resolved == root
            or root.is_relative_to(resolved)
            or (resolved.is_relative_to(root) and not resolved.is_relative_to(runtime_root))
            for root in protected
        ):
            raise ValueError(f"restricted Lead write root overlaps a project root: {resolved}")
        path.mkdir(parents=True, exist_ok=True)
        roots.append(str(resolved))
    return roots


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def codex_lead_argv(write_roots: list[str]) -> list[str]:
    """Codex flags for a restricted Lead: approvals off, sandbox ON with a
    read-only base plus *write_roots*, loopback network for the takkub CLI.
    Replaces the platform autonomy flags (never combined with ``-s``/
    ``sandbox_workspace_write`` — Codex documents the two as exclusive)."""
    grants = ", ".join(f'{_toml_str(root)}="write"' for root in write_roots)
    profile = (
        f'{{extends=":read-only", filesystem={{{grants}}}, '
        'network={enabled=true, domains={"127.0.0.1"="allow", "localhost"="allow"}}}'
    )
    return [
        "--ask-for-approval",
        "never",
        "--no-daemon",
        "--strict-config",
        "-c",
        f"permissions.{PROFILE_NAME}={profile}",
        "-c",
        f'default_permissions="{PROFILE_NAME}"',
        "-c",
        "features.network_proxy=true",
        "--disable",
        "apps",
    ]


def verify_codex_boundary(
    provider_bin: str, cwd: str, env: dict[str, str], write_roots: list[str]
) -> None:
    """Refuse launch unless this Codex installation enforces the exact profile.

    The disposable probe checks both allowed and denied writes without a model
    call. It cannot certify MCP/connector surfaces, which are disabled at spawn.
    """
    config_files = [
        pathlib.Path(env.get("CODEX_HOME") or pathlib.Path.home() / ".codex") / "config.toml"
    ]
    here = pathlib.Path(cwd).resolve()
    config_files.extend(parent / ".codex" / "config.toml" for parent in (here, *here.parents))
    for path in config_files:
        if not path.is_file():
            continue
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        if "sandbox_mode" in config or "sandbox_workspace_write" in config:
            raise RuntimeError(
                f"legacy sandbox configuration overrides permission profiles: {path}"
            )
        if any(
            isinstance(profile, dict) and "sandbox_mode" in profile
            for profile in (config.get("profiles") or {}).values()
        ):
            raise RuntimeError(f"legacy sandbox profile may override permissions: {path}")

    mcp = subprocess.run(
        [
            provider_bin,
            "-c",
            "mcp_servers={}",
            "-c",
            "features.plugins=false",
            "mcp",
            "list",
            "--json",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
        creationflags=SUBPROCESS_NO_WINDOW,
    )
    try:
        if mcp.returncode or json.loads(mcp.stdout) != []:
            raise RuntimeError("restricted Codex Lead still exposes MCP servers")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("cannot verify restricted Codex MCP table") from exc

    from .config import RUNTIME_DIR

    probe_parent = RUNTIME_DIR / "lead-enforcement-probes"
    probe_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=probe_parent) as disposable:
        protected = pathlib.Path(disposable) / "protected.bin"
        protected.write_bytes(b"protected")
        writable = pathlib.Path(write_roots[-1]) / f"boundary-probe-{uuid.uuid4().hex}.bin"
        # The last root is the Lead scratch directory; never probe a source tree.
        config_argv = codex_lead_argv(write_roots)
        overrides = [item for i, item in enumerate(config_argv) if i and config_argv[i - 1] == "-c"]
        prefix = [
            provider_bin,
            "sandbox",
            "-P",
            PROFILE_NAME,
            "-C",
            cwd,
            "--include-managed-config",
            *(part for value in overrides for part in ("-c", value)),
            "--",
            sys.executable,
            "-c",
        ]
        allowed = subprocess.run(
            [
                *prefix,
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'yes')",
                str(writable),
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        if allowed.returncode or not writable.exists() or writable.read_bytes() != b"yes":
            raise RuntimeError("Codex sandbox did not demonstrate its scoped writable root")
        writable.unlink()
        denied = subprocess.run(
            [
                *prefix,
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'changed')",
                str(protected),
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        if denied.returncode == 0 or protected.read_bytes() != b"protected":
            raise RuntimeError("Codex sandbox allowed a write outside scoped roots")


def _claude_rule_path(path: str) -> str:
    """Absolute path in Claude Code's permission-rule form. Rules match POSIX
    paths (``C:\\Users\\x`` is matched as ``/c/Users/x``) and a leading ``//``
    anchors at the filesystem root rather than at the settings file."""
    if re.match(r"^[A-Za-z]:", path):
        win = pathlib.PureWindowsPath(path)
        return f"//{win.drive[0].lower()}/" + "/".join(win.parts[1:])
    return "//" + path.lstrip("/")


def claude_lead_permissions(write_roots: list[str]) -> dict:
    """Claude Code ``permissions`` block for a restricted Lead (#842).

    Paired with ``--permission-mode dontAsk``: Claude Code itself denies every
    call that is neither a built-in read-only action nor allowed here, without
    a prompt. Edits are allowed only under *write_roots*. Shell is limited to
    the cockpit control plane (``takkub``) and GitHub reads/issues; any other
    program — interpreters, redirection into the project, ``git checkout`` —
    is refused (verified live, Claude Code 2.1.296 on Windows 11).

    ``takkub`` subcommands that run project code or write the project from the
    CLI process itself are denied: a Codex Lead runs those inside its sandbox,
    a Claude Lead would not. Bypass and auto modes are disabled so neither the
    model nor a subagent definition can leave the boundary.
    """
    shell_allow = [
        "takkub *",
        "gh issue *",
        "gh pr view *",
        "gh pr list *",
        "gh pr checks *",
        "gh pr diff *",
        "gh run list *",
        "gh run view *",
    ]
    denied_commands = [
        "takkub verify",
        "takkub worktree merge",
        "takkub worktree clean",
        "takkub services",
        "takkub cleanup",
        # `--checkout` switches the project's working tree to a new branch.
        "gh issue develop",
    ]
    # Both the bare command and the command with arguments.
    shell_deny = [rule for cmd in denied_commands for rule in (cmd, f"{cmd} *")]
    allow = [f"{tool}({rule})" for rule in shell_allow for tool in ("Bash", "PowerShell")]
    allow.extend(f"Edit({_claude_rule_path(root)}/**)" for root in write_roots)
    deny = [f"{tool}({rule})" for rule in shell_deny for tool in ("Bash", "PowerShell")]
    return {
        "defaultMode": "dontAsk",
        "allow": allow,
        "deny": deny,
        "disableBypassPermissionsMode": "disable",
        "disableAutoMode": "disable",
    }


#: dontAsk plus the "subagent cannot set bypassPermissions" rule (2.1.267).
CLAUDE_MIN_VERSION = (2, 1, 267)

#: Native Claude tools a restricted Lead may not have: a subagent definition
#: may carry ``permissionMode: acceptEdits``, which was verified to write
#: project files from under a dontAsk parent.
CLAUDE_LEAD_DENIED_TOOLS = ("Agent",)


def verify_claude_version(claude_bin: str, env: dict[str, str]) -> None:
    """Refuse a restricted Lead on a Claude Code too old to hold the boundary."""
    completed = subprocess.run(
        [claude_bin, "--version"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
        creationflags=SUBPROCESS_NO_WINDOW,
    )
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", completed.stdout or "")
    if completed.returncode or match is None:
        raise RuntimeError("cannot determine the Claude Code version")
    if tuple(int(part) for part in match.groups()) < CLAUDE_MIN_VERSION:
        need = ".".join(map(str, CLAUDE_MIN_VERSION))
        raise RuntimeError(f"Claude Code {match.group(0)} is older than {need}")


def enforcement_gap(provider: str, mode: str | None, restricted: bool) -> str:
    """Why a restricted Lead session is NOT actually restricted, or ``""``.

    A restricted Lead needs a boundary the provider itself enforces: Codex's
    OS sandbox, or Claude Code's dontAsk permission engine. Prompt text and
    fail-open hooks do not count, and other providers have neither. Report
    those sessions as unavailable instead of ready."""
    if not restricted or (provider, mode) in _ENFORCED:
        return ""
    return (
        f"restricted Lead policy is not enforced for {provider or 'unknown'} "
        "in this session — restart Lead with a supported protected runtime"
    )


def gate_label(provider: str, mode: str | None, restricted: bool) -> str:
    """Truthful one-word capability for ``takkub context status``."""
    if not restricted:
        return "not_required"
    return _ENFORCED.get((provider, mode), "unenforced")


def codex_denial_records(rollout: pathlib.Path, project_root: str) -> list[dict[str, str]]:
    """Read Codex's own completed tool records without copying commands or output.

    This is an audit adapter, not the enforcement boundary. A missing or
    truncated rollout may omit records; the OS sandbox still denies writes.
    Only a small allowlist of denial signatures is recorded. Targets outside
    the project are reduced to ``<outside-project>`` to avoid logging secrets.
    """
    pending: dict[str, dict[str, str]] = {}
    denied: list[dict[str, str]] = []
    root = pathlib.Path(project_root).resolve()
    try:
        lines = rollout.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return denied
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload") or {}
            call_id = str(payload.get("call_id") or "")
            if not call_id:
                continue
            if payload.get("type") == "custom_tool_call":
                raw = str(payload.get("input") or "")
                tool = "apply_patch" if "tools.apply_patch" in raw else "exec_command"
                match = re.search(r"\*\*\* (?:Update|Add|Delete) File:\s*(.*?)(?:\\n|\r?\n)", raw)
                target = match.group(1).strip() if match else ""
                pending[call_id] = {"tool": tool, "target": target, "input": raw}
                continue
            if payload.get("type") != "custom_tool_call_output":
                continue
            call = pending.pop(call_id, None)
            if call is None:
                continue
            parts = payload.get("output") or []
            texts = []
            exit_code = None
            for part in parts:
                if not isinstance(part, dict):
                    continue
                value = str(part.get("text") or "")
                try:
                    nested = json.loads(value)
                    if isinstance(nested, dict) and isinstance(nested.get("output"), str):
                        value = nested["output"]
                        exit_code = nested.get("exit_code")
                except ValueError:
                    pass
                texts.append(value)
            output = "\n".join(texts)
            lower = output.lower()
            if "patch rejected:" in lower:
                reason = "patch_permission_denied"
            elif "connect_ex=10013" in lower or "network access is denied" in lower:
                reason = "network_permission_denied"
                ip = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b", call["input"])
                call["target"] = ip.group(0) if ip else "<network>"
            elif any(
                marker in lower
                for marker in ("access to the path", "permissionerror", "permission denied")
            ):
                if exit_code == 0:
                    continue
                reason = "filesystem_permission_denied"
                path_match = re.search(r"Access to the path '([^']+)' is denied", output)
                if path_match:
                    call["target"] = path_match.group(1)
            else:
                continue
            target = call["target"]
            if reason != "network_permission_denied":
                try:
                    resolved = pathlib.Path(target).resolve()
                    target = (
                        str(resolved.relative_to(root))
                        if resolved.is_relative_to(root)
                        else "<outside-project>"
                    )
                except (OSError, ValueError):
                    target = "<unresolved>"
            denied.append({"tool": call["tool"], "target": target[:200], "reason": reason})
    return denied
