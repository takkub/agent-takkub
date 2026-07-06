# Security Policy

## Supported versions

Only the latest published version of `agent-takkub` on npm receives security
fixes. Please upgrade (`npm install -g agent-takkub`) before reporting.

| Version | Supported |
| ------- | --------- |
| latest (npm) | ✅ |
| older | ❌ |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

Report privately through GitHub's
[**Report a vulnerability**](https://github.com/takkub/agent-takkub/security/advisories/new)
button (Security tab → Advisories). This opens a private channel with the
maintainers.

Please include:

- affected version (`takkub --version`) and OS (Windows / macOS),
- a description of the issue and its impact,
- steps to reproduce or a proof-of-concept if you have one.

We aim to acknowledge within **72 hours** and to ship a fix or mitigation for
confirmed high-severity issues as quickly as practical. We will credit
reporters in the release notes unless you ask us not to.

## Scope & threat model

`agent-takkub` is a **local desktop cockpit** that orchestrates Claude Code
agents on your own machine. Some behaviours are intentional and **not**
vulnerabilities:

- **Running shell commands.** The cockpit exists to run commands the Lead/agents
  compose on your behalf, with your own user privileges. This is by design.
- **Local IPC socket.** The CLI server binds to **loopback only**
  (`127.0.0.1`), is unreachable from the LAN, and gates mutating commands behind
  per-pane / Lead capability tokens (`secrets.compare_digest`). Reachability
  from other machines would be in scope; a token bypass would be in scope.
- **PATH / registry edits & desktop shortcut** created by the npm `postinstall`
  are local, idempotent, and marker-guarded.

**In scope:** secret exfiltration, remote code execution, the IPC socket
accepting untrusted or unauthenticated input, privilege escalation, a supply-chain
issue in what the package publishes, or a token/auth bypass.

## What we ship

The npm tarball contains only the wrapper (`npm/`), a Python wheel
(`dist/*.whl`), and icon assets — no source, no credentials, no local config.
The package declares **zero runtime npm dependencies**. Credentials are read
from your environment / OS keychain at runtime and are never bundled.
