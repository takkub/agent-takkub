"""Live Preview controller — backend half of #365 phase 5
(`06_LIVE_PREVIEW_SPEC.md` / `12_SECURITY_THREAT_MODEL.md`).

RAM rule (master plan §4): the Preview `QWebEngineView` is **one widget for
the whole app**, lazy-created on first use and destroyed on close — never
one per project. That widget is `preview_widget.py`, frontend-owned and not
built yet. This module is what it will sit on top of: per-project *state*
(mode/target/device/approved), the two security gates
(`12_SECURITY_THREAT_MODEL.md` — loopback-only URLs, containment+extension-
checked files) and the navigation policy a future widget must consult
before letting an in-page navigation through. `PreviewController` itself
stays a thin `QObject` (state + signals only) so it works today, headless,
with no widget attached — the CLI (`takkub preview`, via
`Orchestrator.preview_command`) already exercises the whole state machine.

No privileged bridge, no secret injection: this module never constructs or
touches a `QWebChannel` — that's the widget's job, and the widget must
never register one on an arbitrary remote origin (`navigation_allowed`
below is the gate it's expected to call first).
"""

from __future__ import annotations

import ipaddress
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, pyqtSignal

from . import config
from .project_file_index import resolve_and_contain

_LOOPBACK_LITERAL_HOSTS = frozenset({"localhost"})
ALLOWED_PREVIEW_FILE_EXTENSIONS = frozenset({".html", ".htm"})
DEVICE_PRESETS = ("desktop", "tablet", "mobile")
_DEFAULT_DEVICE = "desktop"


def _log_event(event: str, **details) -> None:
    """Proxy to orchestrator._log_event — lazy import to dodge a cycle, same
    pattern editor_service.py uses for the same reason."""
    _orch = sys.modules.get("agent_takkub.orchestrator")
    if _orch is not None:
        _orch._log_event(event, **details)


def is_loopback_url(url: str) -> bool:
    """True only for an http(s) URL whose host is 127.0.0.0/8, ::1, or the
    literal name "localhost" — the one class of target #365 phase 5 allows
    `open_url` to point the shared Preview widget at (12_SECURITY_THREAT_MODEL.md:
    "no privileged bridge on arbitrary remote pages")."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    if host.lower() in _LOOPBACK_LITERAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _project_source_roots(project_ns: str) -> list[Path]:
    """Label -> path roots configured for *project_ns* in projects.json.
    Deliberately a small independent read (same 3-line lookup
    `project_explorer.project_roots` and `lead_context._allowed_project_roots`
    already each keep their own copy of) rather than importing
    `project_explorer.py` — that module pulls in `PyQt6.QtWidgets`, which a
    pure controller module has no reason to depend on."""
    data = config.load_projects()
    proj = (data.get("projects") or {}).get(project_ns) or {}
    return [Path(p).resolve() for p in (proj.get("paths") or {}).values()]


def approved_artifact_roots(project_ns: str) -> list[Path]:
    """Containment roots for a Preview `open_file` / a published design
    artifact: every configured source root for *project_ns*, plus that
    project's central docs home (`config.project_docs_dir` —
    `RUNTIME_DIR/docs/<project_ns>`, where the CLAUDE.md routing already
    sends design-review output) — the "project roots + docs/design-review
    ที่ project กำหนด" approved-roots pair phase 5's task spec calls for.
    The docs root need not exist yet; containment only needs the path."""
    roots = _project_source_roots(project_ns)
    try:
        roots.append(config.project_docs_dir(project_ns))
    except ValueError:
        pass  # invalid project_ns — _project_source_roots already returned nothing useful
    return roots


def resolve_preview_file(path: Path | str, roots: list[Path]) -> Path:
    """Containment + extension gate for `open_file` / a published `html`/
    `review` design artifact. Raises `PathEscapesRootsError` (traversal) or
    `ValueError` (wrong extension) — never silently narrows the target."""
    resolved = resolve_and_contain(Path(path), roots)
    if resolved.suffix.lower() not in ALLOWED_PREVIEW_FILE_EXTENSIONS:
        raise ValueError(f"preview file must be .html/.htm, got {resolved.name!r}")
    return resolved


@dataclass(frozen=True, slots=True)
class PreviewState:
    project: str
    mode: str  # "url" | "file"
    target: str  # normalized URL, or resolved absolute path as str
    device: str = _DEFAULT_DEVICE
    approved: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class PreviewController(QObject):
    """Per-project Preview state for the whole app. One instance lives on
    the `Orchestrator` (`self._preview_controller`); every project's state
    is keyed by project namespace in `self._states`, matching the "single
    shared widget, per-project state" split the RAM rule requires.

    Signals carry `(project, PreviewState | None)` so a future
    `preview_widget.py` (or several observers — the Explorer's "focus
    preview" action, a status chip) can each decide independently whether a
    given event is about the project they currently have visible.
    """

    opened = pyqtSignal(str, object)  # project, PreviewState
    updated = pyqtSignal(str, object)  # project, PreviewState
    closed = pyqtSignal(str)  # project

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._states: dict[str, PreviewState] = {}
        # #365 phase 10 diagnostics (13_PERFORMANCE_AND_QT_RULES.md rule
        # 10, "navigation policy blocks count") — incremented only by
        # `check_navigation` below, never by the pure `navigation_allowed`
        # helper itself (a future widget can still call that directly
        # without going through this counter).
        self._nav_block_counts: dict[str, int] = {}

    def status(self, project: str) -> PreviewState | None:
        return self._states.get(project)

    def all_states(self) -> dict[str, PreviewState]:
        """Every project with a currently open Preview — `takkub doctor
        --workspace`'s per-project state row."""
        return dict(self._states)

    def check_navigation(self, project: str, target_url: str) -> bool:
        """Convenience wrapper a future `preview_widget.py` can call
        instead of the bare `navigation_allowed` helper: same policy
        decision, plus a diagnostics counter on a refusal. Raises
        `ValueError` if `project` has no open Preview (same as
        `set_device`/`set_approved` above)."""
        current = self._states.get(project)
        if current is None:
            raise ValueError(f"no open preview for project {project!r}")
        allowed = navigation_allowed(current, target_url)
        if not allowed:
            self._nav_block_counts[project] = self._nav_block_counts.get(project, 0) + 1
        return allowed

    def nav_block_counts(self) -> dict[str, int]:
        return dict(self._nav_block_counts)

    def open_url(self, project: str, url: str, *, device: str = _DEFAULT_DEVICE) -> PreviewState:
        if not is_loopback_url(url):
            raise ValueError(
                f"preview URL must be loopback (127.0.0.1/localhost/::1) + port: {url!r}"
            )
        return self._set_state(PreviewState(project=project, mode="url", target=url, device=device))

    def open_file(
        self,
        project: str,
        path: Path | str,
        roots: list[Path],
        *,
        device: str = _DEFAULT_DEVICE,
    ) -> PreviewState:
        resolved = resolve_preview_file(path, roots)
        return self._set_state(
            PreviewState(project=project, mode="file", target=str(resolved), device=device)
        )

    def set_device(self, project: str, device: str) -> PreviewState:
        if device not in DEVICE_PRESETS:
            raise ValueError(f"unknown device preset: {device!r} (want one of {DEVICE_PRESETS})")
        current = self._states.get(project)
        if current is None:
            raise ValueError(f"no open preview for project {project!r}")
        return self._set_state(replace(current, device=device))

    def set_approved(self, project: str, approved: bool) -> PreviewState:
        current = self._states.get(project)
        if current is None:
            raise ValueError(f"no open preview for project {project!r}")
        return self._set_state(replace(current, approved=approved))

    def _set_state(self, state: PreviewState) -> PreviewState:
        is_update = state.project in self._states
        self._states[state.project] = state
        _log_event(
            "workspace.preview.updated" if is_update else "workspace.preview.opened",
            project=state.project,
            mode=state.mode,
            target=state.target,
        )
        (self.updated if is_update else self.opened).emit(state.project, state)
        return state

    def close(self, project: str) -> bool:
        if project not in self._states:
            return False
        del self._states[project]
        _log_event("workspace.preview.closed", project=project)
        self.closed.emit(project)
        return True


def navigation_allowed(current: PreviewState, target_url: str) -> bool:
    """Policy the widget must consult before letting an in-page navigation
    (a link click, a JS redirect, `window.location`) proceed. URL mode
    allows same-origin (scheme+host+port) navigation only — a dev server
    following its own links stays inside the sandbox; anything cross-origin
    is refused. File mode allows staying on the exact open file (an anchor
    jump, a hash-only reload) and nothing else — matching "explicit external
    navigation policy" in `12_SECURITY_THREAT_MODEL.md`: never off to a
    second local file. A caller wanting a genuinely different target goes
    back through `open_url`/`open_file`, not an in-page nav.
    """
    if current.mode == "file":
        return target_url == current.target
    try:
        cur = urlsplit(current.target)
        nxt = urlsplit(target_url)
    except ValueError:
        return False
    return (cur.scheme, cur.hostname, cur.port) == (nxt.scheme, nxt.hostname, nxt.port)
