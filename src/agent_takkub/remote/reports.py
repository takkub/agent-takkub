"""reports.py — Remote Reports store (#367): Lead publishes a standalone
file (HTML dashboard, screenshot, PDF, ...) under a per-file share-token URL
served on the cockpit's own tunnel domain, so a report can be handed to
someone who never gets a Claude Artifact URL or cockpit credentials at all.

Leaf module (X-check C2/U5 — same isolation discipline as `design_actions.
py`): imports only stdlib + `..config`, never Qt, never `http_server`/`api`.
Two callers read/write the same on-disk store with no IPC between them:

  * `cli.py`'s `takkub report publish|list|revoke|rotate` (dynamic
    `importlib.import_module` per the `remote-bolt-on-isolation` contract —
    `cli.py` may never statically import anything under `agent_takkub.
    remote`) does direct file I/O here. No cockpit process needs to be
    running for `publish`/`list`/`revoke`/`rotate` to work — see
    `remote_status_text()`.
  * `http_server.py`'s `/r/<project_ns>/<name>?k=<token>` route re-reads
    `_shares.json` via `resolve()` on every request, so a `publish` that
    happened while the cockpit was closed is picked up the next time it
    (re)starts, with no restart-the-server step of its own.

Store layout (``project_ns`` = the same namespace ``/api/lead/upload``
derives via ``config.validate_name(from_project or "default", "project")``
— see `api.py`'s `lead_upload_image`; reused verbatim here, never
re-implemented):

    <RUNTIME_DIR>/exports/<project_ns>/reports/
      <name>            <- the published file itself (copied in verbatim)
      _shares.json      <- {name: {token, created, expires, label}}
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import config as _config

_SHARES_FILENAME = "_shares.json"

# Deliberately narrow — a report is a document to hand to someone, never an
# executable or an arbitrary blob. Matches the issue's whitelist exactly.
_ALLOWED_EXTENSIONS = frozenset({".html", ".png", ".jpg", ".jpeg", ".svg", ".pdf", ".json", ".md"})

_WARN_BYTES = 5 * 1024 * 1024

# Stored filename: no path separators, no traversal, no drive letters (":"
# is excluded from the allowed charset, which also closes the Windows
# "C:foo" drive-relative case before it ever reaches path arithmetic).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")

_EXPIRES_RE = re.compile(r"^(\d+)([hd])$")

# publish() rejects a report that isn't standalone — a report served under a
# share-token should never make the recipient's browser reach out to a third
# party. Not a full HTML/CSS parser; a case-insensitive regex is cheap to
# reason about and catches the shapes below, but it is a secondary,
# best-effort gate — `_REPORT_CSP_HEADER` (http_server.py)'s `default-src
# 'self'` is what actually enforces this at render/fetch time, so a gap here
# is not by itself exploitable. `//host/...` (protocol-relative) counts as
# external same as `https?://`.
_EXTERNAL_URL_RE = r"(?:https?:)?//"

# <script src=.../<link href=.../<img src=.../<image href=... (SVG's raster
# tag) plus <iframe src=.../<object data=.../<embed src=... (object/embed's
# external ref is also blocked by the CSP's `object-src 'none'`, but iframe
# has no such fallback of its own here — default-src covers it, this regex
# is the extra gate).
_EXTERNAL_ASSET_RE = re.compile(
    r"<\s*(?:script|link|img|image|iframe|object|embed)\b[^>]*\b(?:src|href|data)\s*=\s*"
    r"['\"]?\s*" + _EXTERNAL_URL_RE,
    re.IGNORECASE,
)

# CSS `@import url(...)`/`@import "..."` and any `url(...)` (inline <style>
# block or a style="..." attribute) pointing at an external origin.
_EXTERNAL_CSS_URL_RE = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?\s*"
    + _EXTERNAL_URL_RE
    + r"|url\(\s*['\"]?\s*"
    + _EXTERNAL_URL_RE,
    re.IGNORECASE,
)

# `.html` and `.svg` both get the external-reference check — SVG can carry
# its own inline `<script>`/external `<image>` reference just like HTML, and
# it's on the share extension whitelist too (was previously never checked).
_CHECKED_EXTENSIONS = frozenset({".html", ".svg"})


class ReportError(ValueError):
    """Invalid input at any Remote Reports entry point — bad name/project/
    extension/traversal/expires, non-standalone HTML, or an unknown report.
    Always carries a message safe to print to the CLI caller as-is."""


@dataclass
class ShareRecord:
    name: str
    token: str
    created: str
    expires: str | None
    label: str

    def as_dict(self) -> dict:
        return asdict(self)


def _project_ns(project: str | None) -> str:
    """Same namespace derivation `/api/lead/upload` uses — see module
    docstring. Raises `ReportError`, not the bare `ValueError` `config.
    validate_name` raises, so every Remote Reports entry point has one
    exception type to catch."""
    try:
        return _config.validate_name(project or "default", "project")
    except ValueError:
        raise ReportError(f"invalid project: {project!r}") from None


def reports_root(project: str | None) -> Path:
    return (_config.RUNTIME_DIR / "exports" / _project_ns(project) / "reports").resolve()


def _shares_path(project: str | None) -> Path:
    return reports_root(project) / _SHARES_FILENAME


def _validate_report_name(name: str) -> str:
    name = (name or "").strip()
    if not name or ".." in name or "/" in name or "\\" in name or not _NAME_RE.match(name):
        raise ReportError(f"invalid name: {name!r} — letters/digits/._- only, no path separators")
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
        raise ReportError(f"unsupported extension {ext!r} — allowed: {allowed}")
    return name


def _contained_path(root: Path, name: str) -> Path:
    """Same standard-safe pattern `http_server.py`'s `_serve_static` uses:
    `.resolve()` collapses `..`/symlinks into an absolute path first, then
    `is_relative_to()` verifies containment before any filesystem read/write
    — this is also what closes the "symlink inside reports_root pointing
    outside it" case, not a separate check (resolving *is* dereferencing)."""
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root):
        raise ReportError(f"invalid name: {name!r}")
    return candidate


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_expired(expires_iso: str) -> bool:
    try:
        exp = datetime.fromisoformat(expires_iso)
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return datetime.now(UTC) >= exp


def _parse_expires(expires: str | None) -> str | None:
    """`"30d"`, `"12h"`, `"none"`/empty -> `None` (no expiry). Raises on
    anything else rather than silently treating a typo as "never expires"."""
    if not expires or expires.strip().lower() == "none":
        return None
    match = _EXPIRES_RE.match(expires.strip().lower())
    if not match:
        raise ReportError(f"invalid --expires {expires!r} — use e.g. 30d, 12h, or none")
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * (3600 if unit == "h" else 86400)
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _load_shares(project: str | None) -> dict:
    path = _shares_path(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_shares(project: str | None, data: dict) -> None:
    """Atomic tmp+rename, same pattern `RemoteConfig.save()` uses — a crash
    mid-write can never leave `_shares.json` truncated/corrupt."""
    path = _shares_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def validate_standalone_html(path: Path) -> list[str]:
    """Checked for `.html` and `.svg` files (see `_CHECKED_EXTENSIONS`).
    Raises `ReportError` (reject — not a warning) on an external asset tag
    (`<script>`/`<link>`/`<img>`/`<image>`/`<iframe>`/`<object>`/`<embed>`,
    absolute or protocol-relative) or an external CSS `url(...)`/`@import`:
    a report served under a share-token should be self-contained. This is a
    best-effort regex gate, not the enforcement — `_REPORT_CSP_HEADER`
    (http_server.py)'s `default-src 'self'` is what actually blocks a
    report from reaching a third-party origin at render/fetch time, so a
    shape this regex misses is still blocked there. Returns non-fatal
    warnings (currently just the > 5 MB size heads-up) for the caller to
    print alongside a successful publish."""
    warnings: list[str] = []
    if path.suffix.lower() in _CHECKED_EXTENSIONS:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise ReportError(f"could not read {path}: {exc}") from None
        match = _EXTERNAL_ASSET_RE.search(text) or _EXTERNAL_CSS_URL_RE.search(text)
        if match:
            raise ReportError(
                "report must be standalone (no external script/link/img/iframe/"
                f"object/embed/CSS reference) — found: {match.group(0)[:80]!r}"
            )
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > _WARN_BYTES:
        warnings.append(f"{path.name} is {size / (1024 * 1024):.1f} MB — consider trimming assets")
    return warnings


def publish(
    src: str | Path,
    name: str,
    project: str | None,
    *,
    expires: str | None = None,
    label: str = "",
) -> tuple[ShareRecord, list[str]]:
    """Copy `src` into the project's reports dir as `name`, mint a token (or
    reuse the existing one if `name` was already published — "publish ชื่อเดิม
    = ไฟล์ใหม่ token เดิม"), and persist the registry. Republishing without
    `--expires`/`--label` keeps whatever was already stored for that name
    rather than clearing it."""
    src_path = Path(src)
    if not src_path.is_file():
        raise ReportError(f"source file not found: {src_path}")
    safe_name = _validate_report_name(name)
    warnings = validate_standalone_html(src_path)

    root = reports_root(project)
    root.mkdir(parents=True, exist_ok=True)
    dest = _contained_path(root, safe_name)
    shutil.copyfile(src_path, dest)

    shares = _load_shares(project)
    existing = shares.get(safe_name)
    existing = existing if isinstance(existing, dict) else {}
    token = existing.get("token") or secrets.token_urlsafe(32)
    now = _now_iso()
    record = ShareRecord(
        name=safe_name,
        token=token,
        created=existing.get("created") or now,
        expires=_parse_expires(expires) if expires is not None else existing.get("expires"),
        label=label or existing.get("label", ""),
    )
    shares[safe_name] = record.as_dict()
    _save_shares(project, shares)
    return record, warnings


def list_shares(project: str | None) -> list[ShareRecord]:
    shares = _load_shares(project)
    out: list[ShareRecord] = []
    for name, data in shares.items():
        if not isinstance(data, dict):
            continue
        out.append(
            ShareRecord(
                name=data.get("name") or name,
                token=data.get("token") or "",
                created=data.get("created") or "",
                expires=data.get("expires"),
                label=data.get("label") or "",
            )
        )
    out.sort(key=lambda r: r.name)
    return out


def revoke(name: str, project: str | None, *, delete: bool = False) -> bool:
    """Drop the share record (link dies immediately — `resolve()` can no
    longer find a token to match). `delete=True` also removes the stored
    file; the file is left in place otherwise, matching the issue's
    "revoke = link dies, file stays unless --delete" contract."""
    safe_name = _validate_report_name(name)
    shares = _load_shares(project)
    existed = shares.pop(safe_name, None) is not None
    _save_shares(project, shares)
    if delete:
        root = reports_root(project)
        try:
            dest = _contained_path(root, safe_name)
        except ReportError:
            dest = None
        if dest is not None:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
    return existed


def rotate(name: str, project: str | None) -> ShareRecord:
    """New token for `name` — the old link 404s immediately (`resolve()`
    compares against whatever token is currently stored)."""
    safe_name = _validate_report_name(name)
    shares = _load_shares(project)
    existing = shares.get(safe_name)
    if not isinstance(existing, dict):
        raise ReportError(f"no such report: {safe_name!r}")
    existing["token"] = secrets.token_urlsafe(32)
    shares[safe_name] = existing
    _save_shares(project, shares)
    return ShareRecord(
        name=safe_name,
        token=existing["token"],
        created=existing.get("created") or "",
        expires=existing.get("expires"),
        label=existing.get("label") or "",
    )


def resolve(project_ns: str, name: str, token: str | None) -> Path | None:
    """`http_server.py`'s `/r/` route. Never raises — every failure mode
    (bad project/name, wrong/missing/expired token, missing file,
    disallowed extension, traversal/symlink escape) returns `None`, which
    the route turns into the exact same 404 as "no such report" (§7.5:
    never let a wrong-token guess distinguish "exists" from "doesn't")."""
    if not token:
        return None
    try:
        safe_name = _validate_report_name(name)
        root = reports_root(project_ns)
    except ReportError:
        return None
    shares = _load_shares(project_ns)
    record = shares.get(safe_name)
    if not isinstance(record, dict):
        return None
    expected_token = record.get("token")
    if not isinstance(expected_token, str) or not expected_token:
        return None
    if not hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8")):
        return None
    expires = record.get("expires")
    if isinstance(expires, str) and _is_expired(expires):
        return None
    try:
        candidate = _contained_path(root, safe_name)
    except ReportError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def build_url(project_ns: str, name: str, token: str) -> str:
    """Full share URL on the cockpit's own tunnel domain. Empty string if
    remote control has never completed its first boot (`public_url`/
    `secret_path` not minted yet) — callers print the local file path
    instead of a URL that would never authenticate (`RemoteConfig.
    pairing_url`'s same "never guess" rule)."""
    from .config import RemoteConfig

    cfg = RemoteConfig.load()
    if not (cfg.public_url and cfg.secret_path):
        return ""
    return f"{cfg.public_url.rstrip('/')}/{cfg.secret_path}/r/{project_ns}/{name}?k={token}"


def remote_status_text() -> str:
    """Thai status line `takkub report publish|list|rotate` print every
    time (user directive 2026-08-23, forwarded via Lead): a report URL only
    resolves while remote control is both enabled AND its tunnel is
    actually up right now, so callers must never let a stale/unreachable
    link look identical to a live one. Disk-only (no IPC to the running
    cockpit process) — `RemoteConfig.load()` reads `remote.json`, `tunnel.
    is_tunnel_alive()` reads the tunnel's own pid file."""
    from . import tunnel as _tunnel_mod
    from .config import RemoteConfig

    cfg = RemoteConfig.load()
    if cfg.enabled and _tunnel_mod.is_tunnel_alive():
        return "Remote: เปิดอยู่ (tunnel up) → URL ใช้ได้"
    return (
        "Remote: ปิดอยู่ → ลิงก์นี้ยังเปิดจากนอกไม่ได้ เปิด Remote ก่อน "
        "(เปิดคอกพิต → Settings → Remote — ยังไม่มีคำสั่งเปิดผ่าน CLI)"
    )
