"""tunnel.py — spawn the user's tunnel process (§6.6, dual-mode).

Mode A (`type: "cloudflared"`, named tunnel + domain): the user drops in
their `credentials_json` + `public_url` + `bind_port`; we read the
`TunnelID` out of the credentials file, render `runtime/tunnel/config.yml`
from a template, and spawn `cloudflared ... run` directly. The URL is
`public_url` and is never captured from output.

Mode B (`type: "bat"`, quick tunnel / any other provider): the user's own
script is spawned with the port as arg + `TAKKUB_REMOTE_PORT` env, and its
stdout is scraped for a public URL via regex.

Both modes: spawned through `cmd /d /c` on Windows / `/bin/sh -c` on posix
for predictable quoting (X-check 5.1), and torn down with
`pty_session._tree_kill` (X-check 5.2) so a `cloudflared`/provider
grandchild the wrapping shell doesn't directly own is never orphaned.

Note: `TunnelConfig.credentials_json` is reused as "the one file/script path
the user supplied" for both modes (the cloudflared credentials JSON in Mode
A, the .bat/.sh script path in Mode B) — this matches the field the P0
scaffold already froze (`remote/config.py`), not a new convention invented
here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from ..config import RUNTIME_DIR
from ..pty_session import _tree_kill
from .config import TunnelConfig

_log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"https://[^\s]+\.(?:trycloudflare\.com|ngrok[^\s]*|loca\.lt|lhr\.life|ts\.net)"
)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_CONFIG_TEMPLATE = """\
tunnel: {tunnel_id}
credentials-file: {credentials_json}
protocol: auto
ingress:
  - hostname: {hostname}
    service: http://localhost:{port}
  - service: http_status:404
"""


class TunnelError(RuntimeError):
    pass


def _read_tunnel_id(credentials_json: str) -> str:
    try:
        data = json.loads(Path(credentials_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TunnelError(f"can't read tunnel credentials: {exc}") from exc
    tunnel_id = data.get("TunnelID") if isinstance(data, dict) else None
    if not tunnel_id:
        raise TunnelError("credentials json is missing TunnelID")
    return tunnel_id


def _write_named_config(tunnel: TunnelConfig, public_url: str, port: int) -> Path:
    tunnel_id = _read_tunnel_id(tunnel.credentials_json)
    # localhost, always — never host.docker.internal or similar; loopback is
    # the whole point of "we bind LocalHost only" (§7.2).
    hostname = re.sub(r"^https?://", "", public_url).rstrip("/")
    out_dir = RUNTIME_DIR / "tunnel"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "config.yml"
    out_path.write_text(
        _CONFIG_TEMPLATE.format(
            tunnel_id=tunnel_id,
            credentials_json=tunnel.credentials_json,
            hostname=hostname,
            port=port,
        ),
        encoding="utf-8",
    )
    return out_path


def _spawn(argv: list[str], extra_env: dict | None = None) -> subprocess.Popen:
    """5.1: platform-specific launch for predictable quoting — not because a
    direct `Popen([exe, *args])` is known to fail on this host."""
    env = {**os.environ, **extra_env} if extra_env else None
    if sys.platform == "win32":
        launch = ["cmd", "/d", "/c", subprocess.list2cmdline(argv)]
        return subprocess.Popen(
            launch,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
            env=env,
        )
    launch = ["/bin/sh", "-c", shlex.join(argv)]
    return subprocess.Popen(
        launch,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group, mirrors ptyprocess's setsid (5.2)
        env=env,
    )


class Tunnel:
    """Owns one running tunnel subprocess. `start()`/`stop()` only —
    `RemoteControl` decides when those fire."""

    def __init__(self, tunnel_config: TunnelConfig, public_url: str, port: int) -> None:
        self._config = tunnel_config
        self._public_url = public_url
        self._port = port
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        # Mode A: URL is known upfront. Mode B: filled in by _scan_for_url.
        self.captured_url: str | None = public_url or None

    def start(self) -> None:
        if self._config.type == "cloudflared":
            self._start_named()
        else:
            self._start_bat()

    def _cloudflared_bin(self) -> str:
        return self._config.cloudflared_bin or shutil.which("cloudflared") or "cloudflared"

    def _start_named(self) -> None:
        if not self._config.credentials_json or not self._public_url:
            raise TunnelError("named tunnel needs credentials_json + public_url")
        config_path = _write_named_config(self._config, self._public_url, self._port)
        argv = [
            self._cloudflared_bin(),
            "tunnel",
            "--config",
            str(config_path),
            "--credentials-file",
            self._config.credentials_json,
            "run",
        ]
        self._proc = _spawn(argv)
        self._drain_output()

    def _start_bat(self) -> None:
        script = self._config.credentials_json
        if not script:
            raise TunnelError("bat tunnel needs a script path")
        argv = [script, str(self._port)]
        self._proc = _spawn(argv, extra_env={"TAKKUB_REMOTE_PORT": str(self._port)})
        self._reader = threading.Thread(target=self._scan_for_url, daemon=True)
        self._reader.start()

    def _drain_output(self) -> None:
        """Named-tunnel mode doesn't need the URL scraped, but the child's
        stdout pipe must still be drained or cloudflared blocks once its own
        log output fills the pipe buffer."""

        def _drain() -> None:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            for _ in proc.stdout:
                pass

        self._reader = threading.Thread(target=_drain, daemon=True)
        self._reader.start()

    def _scan_for_url(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            if self.captured_url is None:
                match = _URL_RE.search(line.decode("utf-8", errors="replace"))
                if match:
                    self.captured_url = match.group(0)
                    _log.info("remote tunnel URL captured")

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        _tree_kill(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
