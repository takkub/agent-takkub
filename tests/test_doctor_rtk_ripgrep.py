"""doctor.check_rtk_ripgrep (#402) — rtk (the external grep proxy every
pane's Bash hook routes through) prints "Failed to resolve 'rg' via PATH,
falling back to direct exec" on stderr on EVERY call when ripgrep isn't
installed. rtk is a closed external binary we can't patch, so the only real
fix is telling the user to install ripgrep — this check surfaces that in
`takkub doctor` instead of leaving them to notice the per-call noise.
"""

from __future__ import annotations

import inspect
import sys

from agent_takkub import doctor, rtk_helper
from agent_takkub.doctor import Finding, Status, check_rtk_ripgrep, run_all_checks


def test_no_findings_when_rtk_itself_is_not_installed(monkeypatch):
    monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    assert check_rtk_ripgrep() == []


def test_no_findings_when_ripgrep_is_already_on_path(monkeypatch):
    monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/usr/bin/rg" if "rg" in name else None
    )

    assert check_rtk_ripgrep() == []


def test_warns_when_rtk_present_but_ripgrep_missing(monkeypatch):
    monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    findings = check_rtk_ripgrep()

    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.category == "rtk"
    assert f.status == Status.WARN
    assert "ripgrep" in f.detail.lower()
    assert "#402" in f.detail
    assert f.fix_hint  # a real install command, not left blank


def test_fix_hint_is_platform_specific(monkeypatch):
    monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    monkeypatch.setattr(sys, "platform", "win32")
    win_hint = check_rtk_ripgrep()[0].fix_hint
    assert "winget" in win_hint

    monkeypatch.setattr(sys, "platform", "darwin")
    mac_hint = check_rtk_ripgrep()[0].fix_hint
    assert "brew" in mac_hint

    monkeypatch.setattr(sys, "platform", "linux")
    linux_hint = check_rtk_ripgrep()[0].fix_hint
    assert "apt" in linux_hint or "dnf" in linux_hint or "pacman" in linux_hint


def test_checks_rg_dot_exe_too_not_just_bare_rg(monkeypatch):
    """Windows resolves the ripgrep binary as `rg.exe`, not bare `rg` —
    must not false-positive-warn on a machine that has it installed."""
    monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "C:\\rg.exe" if name == "rg.exe" else None
    )

    assert check_rtk_ripgrep() == []


def test_is_registered_in_run_all_checks_default_set():
    source = inspect.getsource(run_all_checks)
    assert "check_rtk_ripgrep" in source


def test_run_all_checks_never_crashes_when_rtk_missing_entirely(monkeypatch):
    """run_all_checks wraps every check in try/except — but check_rtk_ripgrep
    itself must not need that safety net for the common case (rtk not
    installed at all)."""
    monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)

    findings = run_all_checks()

    assert not any(
        f.category == "doctor" and f.name == "check_rtk_ripgrep" and f.status == Status.FAIL
        for f in findings
    )
