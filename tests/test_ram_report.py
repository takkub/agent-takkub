"""#364 lever 6 — `ram_report.collect_ram_report`'s pure tree-walk logic,
exercised against a fake psutil (no real processes spawned)."""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub.ram_report import collect_ram_report

MB = 1_000_000


class _FakeProc:
    def __init__(self, pid: int, ppid: int, name: str, rss: int, *, children=None) -> None:
        self.pid = pid
        self._ppid = ppid
        self._name = name
        self._rss = rss
        self._children = children or []

    def ppid(self) -> int:
        return self._ppid

    def name(self) -> str:
        return self._name

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)

    def children(self, recursive: bool = False):
        return self._children


class _FakePsutil:
    def __init__(self, by_pid: dict, *, total: int, available: int) -> None:
        self._by_pid = by_pid
        self._total = total
        self._available = available

    def Process(self, pid: int):
        if pid not in self._by_pid:
            raise RuntimeError(f"no such process {pid}")
        return self._by_pid[pid]

    def virtual_memory(self):
        return SimpleNamespace(total=self._total, available=self._available)


def _build_tree() -> tuple[_FakePsutil, int]:
    main_pid = 100
    node_child = _FakeProc(201, 200, "node.exe", 80 * MB)
    pane_qtwe = _FakeProc(202, 200, "QtWebEngineProcess.exe", 99 * MB)
    claude_cli = _FakeProc(200, main_pid, "claude.exe", 550 * MB, children=[])
    codex_cli = _FakeProc(300, main_pid, "codex.exe", 400 * MB)
    shared_qtwe = _FakeProc(400, main_pid, "QtWebEngineProcess.exe", 99 * MB)
    all_descendants = [claude_cli, node_child, pane_qtwe, codex_cli, shared_qtwe]
    main_proc = _FakeProc(main_pid, 1, "pythonw.exe", 300 * MB, children=all_descendants)

    by_pid = {
        main_pid: main_proc,
        200: claude_cli,
        201: node_child,
        202: pane_qtwe,
        300: codex_cli,
        400: shared_qtwe,
    }
    psutil_module = _FakePsutil(by_pid, total=16_000 * MB, available=4_000 * MB)
    return psutil_module, main_pid


def test_collect_ram_report_attributes_descendants_to_the_owning_pane() -> None:
    psutil_module, main_pid = _build_tree()
    pane_specs = [
        {"role": "backend", "project": "proj1", "provider": "claude", "pid": 200},
        {"role": "codex", "project": "proj1", "provider": "codex", "pid": 300},
    ]

    report = collect_ram_report(
        pane_specs,
        main_pid=main_pid,
        governor_min_ram_percent=12.0,
        psutil_module=psutil_module,
    )

    by_role = {row["role"]: row for row in report["panes"]}
    backend = by_role["backend"]
    assert backend["cli_rss_bytes"] == 550 * MB
    assert backend["node_children_rss_bytes"] == 80 * MB
    assert backend["node_children_count"] == 1
    assert backend["qtwebengine_rss_bytes"] == 99 * MB
    assert backend["total_bytes"] == (550 + 80 + 99) * MB
    assert backend["note"] == ""

    codex = by_role["codex"]
    assert codex["cli_rss_bytes"] == 400 * MB
    assert codex["node_children_rss_bytes"] == 0
    assert codex["qtwebengine_rss_bytes"] == 0
    assert codex["total_bytes"] == 400 * MB


def test_collect_ram_report_never_guesses_an_unattributable_qtwebengine() -> None:
    """The pid=400 QtWebEngineProcess is a direct child of the cockpit root,
    not of any pane's CLI subtree — it must land in `shared_qtwebengine`,
    never silently folded into a pane's total."""
    psutil_module, main_pid = _build_tree()
    pane_specs = [
        {"role": "backend", "project": "proj1", "provider": "claude", "pid": 200},
        {"role": "codex", "project": "proj1", "provider": "codex", "pid": 300},
    ]

    report = collect_ram_report(pane_specs, main_pid=main_pid, psutil_module=psutil_module)

    assert report["shared_qtwebengine"]["rss_bytes"] == 99 * MB
    assert report["shared_qtwebengine"]["process_count"] == 1
    assert report["main_process"] == {"pid": main_pid, "rss_bytes": 300 * MB}
    assert report["total_panes_bytes"] == (550 + 80 + 99 + 400) * MB
    assert report["total_cockpit_bytes"] == (550 + 80 + 99 + 400 + 300 + 99) * MB
    assert report["machine_available_percent"] == 25.0
    assert report["governor_min_available_ram_percent"] is None


def test_collect_ram_report_passes_through_discarded_flag() -> None:
    """#364 lever 1: `discarded` is caller-supplied (this module never
    touches TerminalWidget) — just verify it round-trips per pane, defaulting
    to False when the caller's spec omits it."""
    psutil_module, main_pid = _build_tree()
    pane_specs = [
        {
            "role": "backend",
            "project": "proj1",
            "provider": "claude",
            "pid": 200,
            "discarded": True,
        },
        {"role": "codex", "project": "proj1", "provider": "codex", "pid": 300},
    ]

    report = collect_ram_report(pane_specs, main_pid=main_pid, psutil_module=psutil_module)

    by_role = {row["role"]: row for row in report["panes"]}
    assert by_role["backend"]["discarded"] is True
    assert by_role["codex"]["discarded"] is False


def test_collect_ram_report_marks_missing_or_unknown_pids_instead_of_guessing() -> None:
    psutil_module, main_pid = _build_tree()
    pane_specs = [
        {"role": "no-pid", "project": "proj1", "provider": "claude", "pid": None},
        {"role": "gone", "project": "proj1", "provider": "claude", "pid": 999},
    ]

    report = collect_ram_report(pane_specs, main_pid=main_pid, psutil_module=psutil_module)

    by_role = {row["role"]: row for row in report["panes"]}
    assert by_role["no-pid"]["note"] == "no pid recorded"
    assert by_role["no-pid"]["total_bytes"] == 0
    assert by_role["gone"]["note"] == "process not found (exited?)"
    assert by_role["gone"]["total_bytes"] == 0


class _CountingPsutil(_FakePsutil):
    """Fake psutil that ALSO exposes the platform-module `ppid_map` primitive
    real psutil has, counting how often it is hit."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ppid_map_calls = 0
        outer = self

        class _Platform:
            @staticmethod
            def ppid_map() -> dict[int, int]:
                outer.ppid_map_calls += 1
                return {pid: proc._ppid for pid, proc in outer._by_pid.items()}

        self._psplatform = _Platform()
        for proc in self._by_pid.values():
            proc.ppid_calls = 0
            _orig = proc.ppid

            def _counting_ppid(_orig=_orig, _proc=proc) -> int:
                _proc.ppid_calls += 1
                return _orig()

            proc.ppid = _counting_ppid


def test_collect_ram_report_snapshots_the_ppid_map_once_instead_of_per_process() -> None:
    """Lead-pane input-lag fix (2026-08-26): psutil's Windows `Process.ppid()`
    re-enumerates the whole process table inside a GIL-holding C call on
    EVERY invocation, so the old per-descendant loop held the GIL for
    0.7–2 s per RAM-chip tick (py-spy --gil: 51% of all GIL samples) and
    the Qt main thread — Lead's keystroke echo — waited it out. One
    `ppid_map()` snapshot must replace the per-process calls."""
    psutil_module, main_pid = _build_tree()
    counting = _CountingPsutil(psutil_module._by_pid, total=16_000 * MB, available=4_000 * MB)
    pane_specs = [
        {"role": "backend", "project": "proj1", "provider": "claude", "pid": 200},
        {"role": "codex", "project": "proj1", "provider": "codex", "pid": 300},
    ]

    report = collect_ram_report(pane_specs, main_pid=main_pid, psutil_module=counting)

    assert counting.ppid_map_calls == 1
    assert all(proc.ppid_calls == 0 for proc in counting._by_pid.values())
    # and the tree attribution is byte-identical to the per-process path
    by_role = {row["role"]: row for row in report["panes"]}
    assert by_role["backend"]["total_bytes"] == (550 + 80 + 99) * MB
    assert report["shared_qtwebengine"]["process_count"] == 1


def test_collect_ram_report_falls_back_to_per_process_ppid_without_ppid_map() -> None:
    """A psutil without the platform primitive (or one whose snapshot misses
    a pid that appeared between the two enumerations) still resolves the
    parent via `Process.ppid()` — never silently drops a descendant."""
    psutil_module, main_pid = _build_tree()
    assert not hasattr(psutil_module, "_psplatform")
    pane_specs = [{"role": "backend", "project": "proj1", "provider": "claude", "pid": 200}]

    report = collect_ram_report(pane_specs, main_pid=main_pid, psutil_module=psutil_module)

    assert report["panes"][0]["total_bytes"] == (550 + 80 + 99) * MB
