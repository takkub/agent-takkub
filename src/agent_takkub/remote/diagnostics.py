"""remote diagnostics (#193): the multi-instance dev/prod port collision made
a whole cockpit instance's remote link "ตายเงียบ" (dies silently — no error,
just a QR code that never connects) because nothing verified that:

  1. the port this instance actually bound is the one it *asked* for
     (`describe_port_owner` — who's actually holding a taken port, so a
     collision reads as "port 9999 in use by pid 11748 (pythonw.exe)"
     instead of nothing);
  2. the pairing URL it's about to show really serves this cockpit
     (`probe_http` — a real GET, not "the subprocess launched fine");
  3. the tunnel process that's actually running was launched with a config
     pointing at the SAME hostname `public_url` claims (`check_ingress_mismatch`
     — catches a stale/orphaned cloudflared, see `tunnel.reap_orphan_tunnel`,
     still routing an old hostname while `remote.json` has since changed).

All three are read-only/best-effort by design: a failure here must never be
mistaken for "the tunnel is broken" — it only means this particular
diagnostic couldn't run (network down, psutil access denied, etc).
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

_log = logging.getLogger(__name__)

_DEFAULT_LOCAL_TIMEOUT_S = 2.0
_DEFAULT_PUBLIC_TIMEOUT_S = 5.0


def describe_port_owner(port: int) -> str | None:
    """Best-effort "who's listening on 127.0.0.1:<port> right now" — used to
    turn a silent bind-fallback into a clear message ("port 9999 already in
    use by pid 11748 (pythonw.exe)"). Returns None if nothing is listening,
    or if psutil can't tell (permission denied — not fatal, caller just
    shows a plainer message)."""
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.laddr
                and conn.laddr.port == port
                and conn.status == psutil.CONN_LISTEN
                and conn.pid
            ):
                try:
                    name = psutil.Process(conn.pid).name()
                except psutil.Error:
                    name = "unknown process"
                return f"pid {conn.pid} ({name})"
        return None
    except Exception:
        _log.exception("remote diagnostics: describe_port_owner failed")
        return None


def probe_http(url: str, timeout: float) -> tuple[bool, str]:
    """A real GET against `url`. Any HTTP response (even a 4xx from a wrong
    secret path) proves *something* is answering; a network/DNS/TLS/timeout
    failure means it doesn't. Never raises — always returns a result."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # A response DID come back — the server exists, just not happy about
        # this particular request (still proves reachability).
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def probe_local(
    port: int, secret_path: str, timeout: float = _DEFAULT_LOCAL_TIMEOUT_S
) -> tuple[bool, str]:
    """Loopback probe — proves this instance's HTTP server really is bound
    and serving on the port it claims (#193 item 1)."""
    return probe_http(f"http://127.0.0.1:{port}/{secret_path}/", timeout)


def probe_public(
    public_url: str, secret_path: str, timeout: float = _DEFAULT_PUBLIC_TIMEOUT_S
) -> tuple[bool, str]:
    """Probe the actual public pairing URL through the tunnel edge (#193
    item 2) — proves the link the QR code shows really routes here, not just
    that a tunnel process happens to be running."""
    return probe_http(f"{public_url.rstrip('/')}/{secret_path}/", timeout)


def check_ingress_mismatch(public_url: str, config_yml_path: Path) -> str | None:
    """#193 item 3: compare `public_url`'s hostname against the ingress
    hostname baked into the currently-on-disk named-tunnel `config.yml`. A
    mismatch means whatever cloudflared process is running was launched
    against a DIFFERENT hostname than what `remote.json`/the UI now claims
    (typically a stale/orphaned process from before a public_url change —
    see `tunnel.reap_orphan_tunnel`). Returns None when there's nothing to
    compare (no config.yml yet, or nothing parses) — that's not a mismatch,
    just no evidence either way."""
    if not public_url or not config_yml_path.is_file():
        return None
    try:
        data = yaml.safe_load(config_yml_path.read_text(encoding="utf-8"))
        ingress = data.get("ingress") if isinstance(data, dict) else None
        current_hostname = ingress[0].get("hostname") if ingress else None
        if not current_hostname:
            return None
        wanted_hostname = urllib.parse.urlsplit(public_url).hostname
        if wanted_hostname and current_hostname != wanted_hostname:
            return (
                f"public_url hostname ({wanted_hostname!r}) does not match the "
                f"tunnel config on disk ({current_hostname!r}) — the running "
                "cloudflared process (if any) may still be routing the old hostname."
            )
        return None
    except Exception:
        _log.exception("remote diagnostics: check_ingress_mismatch failed")
        return None
