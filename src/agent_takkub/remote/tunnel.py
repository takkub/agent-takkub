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

H-E (outlive-crash defense): a graceful `Tunnel.stop()` (via `_tree_kill`)
only covers the case where our own process is still alive to call it.
Two more layers cover the case where it isn't:
  * Windows: the spawned process is assigned to a Job Object created with
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` — the OS closes our handles
    (including the job's) whenever this process dies, by any means, which
    kills every process still in the job. Kernel-enforced; survives a hard
    crash that no signal handler gets a chance to run for.
  * POSIX: `_spawn`'s `start_new_session=True` already makes the child the
    leader of its own process group (mirrors ptyprocess's `setsid`), so
    `_tree_kill`'s `killpg` reaps the whole descendant tree in one signal —
    there's no portable POSIX equivalent of Windows' kernel-level
    parent-death notification available here (Linux's `PR_SET_PDEATHSIG`
    needs a `preexec_fn`, which is unsafe in a multi-threaded process —
    this cockpit has several PTY/HTTP worker threads running by the time a
    tunnel spawns). The atexit/signal/`aboutToQuit` hooks wired into
    `RemoteControl`/`app.py`'s `_kill_all` are the POSIX defense-in-depth
    layer instead: they cover every graceful-ish termination path
    (Ctrl+C, SIGTERM, quit) even when this process wasn't the one that
    called `Tunnel.stop()` for cockpit-generic reasons.

Note: `TunnelConfig.credentials_json` is reused as "the one file/script path
the user supplied" for both modes (the cloudflared credentials JSON in Mode
A, the .bat/.sh script path in Mode B) — this matches the field the P0
scaffold already froze (`remote/config.py`), not a new convention invented
here.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import yaml

from ..config import RUNTIME_DIR
from ..pty_session import _tree_kill
from .config import TunnelConfig

_log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"https://[^\s]+\.(?:trycloudflare\.com|ngrok[^\s]*|loca\.lt|lhr\.life|ts\.net)"
)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# H-D: strict hostname shape (labels of alnum/hyphen, no leading/trailing
# hyphen, 1-63 chars each) — anything that doesn't fully match this can't be
# a raw newline/colon/space smuggled through `urlsplit`'s lenient parsing.
_HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")
_TUNNEL_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class TunnelError(RuntimeError):
    pass


def _validate_public_url(public_url: str) -> str:
    """H-D: `public_url` ends up as the ingress hostname in a generated YAML
    config. Reject anything that isn't a bare `https://<hostname>` — no
    path/query/fragment/userinfo, no port, hostname matching a strict
    allowlist regex — so it can't smuggle extra YAML keys (a new ingress
    rule, a retargeted service) into the file. Returns the validated
    hostname.

    The control-character check runs on the raw string, before
    `urlsplit`: newer `urllib.parse` silently strips `\\t`/`\\r`/`\\n` while
    parsing (its own hardening against header/URL injection), which would
    otherwise make an injection attempt merely *look* like a harmless
    hostname by the time this function ever sees it split apart."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in public_url):
        raise TunnelError("public_url contains control characters")
    parsed = urllib.parse.urlsplit(public_url)
    if parsed.scheme != "https":
        raise TunnelError("public_url must be an https:// URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise TunnelError("public_url must be a bare https hostname (no path/query/fragment)")
    hostname = parsed.hostname or ""
    if not _HOSTNAME_RE.match(hostname):
        raise TunnelError(f"public_url has an invalid hostname: {hostname!r}")
    return hostname


def _read_tunnel_id(credentials_json: str) -> str:
    # H-D: absolute path only — this string is written verbatim into the
    # generated config.yml's `credentials-file:` value.
    if not Path(credentials_json).is_absolute():
        raise TunnelError("credentials_json must be an absolute path")
    try:
        data = json.loads(Path(credentials_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TunnelError(f"can't read tunnel credentials: {exc}") from exc
    tunnel_id = data.get("TunnelID") if isinstance(data, dict) else None
    if not tunnel_id or not _TUNNEL_ID_RE.match(tunnel_id):
        raise TunnelError("credentials json is missing a valid TunnelID (UUID)")
    return tunnel_id


def _write_named_config(tunnel: TunnelConfig, public_url: str, port: int) -> Path:
    # H-D: build the config as a dict and let `yaml.safe_dump` handle all
    # escaping — no more hand-rolled string templating that a newline/colon
    # in any of these values could break out of.
    hostname = _validate_public_url(public_url)
    tunnel_id = _read_tunnel_id(tunnel.credentials_json)
    config = {
        "tunnel": tunnel_id,
        "credentials-file": tunnel.credentials_json,
        "protocol": "auto",
        "ingress": [
            # localhost, always — never host.docker.internal or similar;
            # loopback is the whole point of "we bind LocalHost only" (§7.2).
            {"hostname": hostname, "service": f"http://localhost:{port}"},
            {"service": "http_status:404"},
        ],
    }
    out_dir = RUNTIME_DIR / "tunnel"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "config.yml"
    out_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
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


if sys.platform == "win32":

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("RO", "WO", "OO", "RT", "WT", "OT")]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOBOBJECTINFOCLASS_EXTENDED_LIMIT = 9


def _create_kill_on_close_job() -> int | None:
    """H-E, Windows: a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
    If this process ever dies without running `Tunnel.stop()` first — a
    crash, a hard kill, power loss of the parent — Windows itself closes
    every handle this process held, including the job handle, which then
    kills every process still assigned to the job (the tunnel + whatever it
    spawned). This is kernel-enforced and survives death modes no
    atexit/signal handler ever could; the atexit/signal hooks in
    `RemoteControl`/`app.py` cover the *graceful* shutdown paths instead.
    Best-effort: never raises, a failure here just means H-E's Windows layer
    is absent and the process-group + atexit/signal layer is all that's left.
    """
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            _JOBOBJECTINFOCLASS_EXTENDED_LIMIT,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job: int, pid: int) -> bool:
    """Returns whether the process is actually now protected by `job` — a
    caller that ignores this and assumes success believes a crash kills the
    child when in fact assignment silently failed (e.g. OpenProcess denied,
    already assigned to another job on old Windows without job nesting)."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


class Tunnel:
    """Owns one running tunnel subprocess. `start()`/`stop()` only —
    `RemoteControl` decides when those fire."""

    def __init__(self, tunnel_config: TunnelConfig, public_url: str, port: int) -> None:
        self._config = tunnel_config
        self._public_url = public_url
        self._port = port
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._job: int | None = None
        # Mode A: URL is known upfront. Mode B: filled in by _scan_for_url.
        self.captured_url: str | None = public_url or None

    def start(self) -> None:
        if self._config.type == "cloudflared":
            self._start_named()
        elif self._config.type == "quick":
            self._start_quick()
        elif self._config.type == "ngrok":
            self._start_ngrok()
        else:
            self._start_bat()

    def _cloudflared_bin(self) -> str:
        return self._config.cloudflared_bin or shutil.which("cloudflared") or "cloudflared"

    def _ngrok_bin(self) -> str:
        return self._config.ngrok_bin or shutil.which("ngrok") or "ngrok"

    def _own_job_if_windows(self) -> None:
        if sys.platform != "win32" or self._proc is None:
            return
        job = _create_kill_on_close_job()
        if job is None:
            return
        if _assign_to_job(job, self._proc.pid):
            self._job = job
        else:
            _log.warning("remote tunnel: process not assigned to kill-on-close job object")
            try:
                ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]
            except Exception:
                pass

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
        self._own_job_if_windows()
        self._drain_output()

    def _start_quick(self) -> None:
        """Mode "quick" (addendum, no-domain path): cockpit spawns
        cloudflared's own quick-tunnel mode directly — no credentials file,
        no config.yml, no `public_url` up front. The random
        `*.trycloudflare.com` URL cloudflared prints to stdout is scraped by
        the same `_scan_for_url` Mode B (bat) already uses."""
        argv = [self._cloudflared_bin(), "tunnel", "--url", f"http://localhost:{self._port}"]
        self._proc = _spawn(argv)
        self._own_job_if_windows()
        self._reader = threading.Thread(target=self._scan_for_url, daemon=True)
        self._reader.start()

    def _start_ngrok(self) -> None:
        """ngrok provider (addendum): "random" scrapes the assigned
        `*.ngrok-free.app` URL from stdout via `_scan_for_url`, same as
        quick-tunnel mode; "fixed" passes the user's reserved domain via
        `--url` — its `captured_url` is already set from `public_url`
        (`RemoteControl._start()`/dialog set it upfront), so the reader
        thread only drains stdout in that case. Authtoken setup (`ngrok
        config add-authtoken`) is the dialog's job at Enable time, not
        this class's — by the time `start()` runs, ngrok is assumed to
        already be authenticated on this machine."""
        if self._config.url_mode == "fixed":
            domain = self._config.ngrok_domain.strip()
            if not domain or not _HOSTNAME_RE.match(domain):
                raise TunnelError("ngrok fixed mode needs a valid domain")
            argv = [
                self._ngrok_bin(),
                "http",
                str(self._port),
                "--url",
                f"https://{domain}",
                "--log",
                "stdout",
            ]
        else:
            argv = [self._ngrok_bin(), "http", str(self._port), "--log", "stdout"]
        self._proc = _spawn(argv)
        self._own_job_if_windows()
        self._reader = threading.Thread(target=self._scan_for_url, daemon=True)
        self._reader.start()

    def _start_bat(self) -> None:
        script = self._config.credentials_json
        if not script:
            raise TunnelError("bat tunnel needs a script path")
        argv = [script, str(self._port)]
        self._proc = _spawn(argv, extra_env={"TAKKUB_REMOTE_PORT": str(self._port)})
        self._own_job_if_windows()
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
        job = self._job
        self._job = None
        if job is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]
            except Exception:
                pass
        if proc is None:
            return
        _tree_kill(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
