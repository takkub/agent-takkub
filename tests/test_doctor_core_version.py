"""doctor.check_core_version_compat — opt-in Core V2 Schema/Adapter/Compat
check (#309 Phase 4). Must stay OUT of run_all_checks()'s default set so a
plain `takkub doctor` is byte-identical to before this landed."""

from __future__ import annotations

import inspect

from agent_takkub.doctor import Finding, Status, check_core_version_compat, run_all_checks


def test_check_core_version_compat_never_fails():
    findings = check_core_version_compat()
    assert findings, "expected at least one finding per registered provider"
    for f in findings:
        assert isinstance(f, Finding)
        assert f.status != Status.FAIL
        assert f.category == "version"


def test_check_core_version_compat_covers_every_registered_provider():
    from agent_takkub.provider_spec import PROVIDER_REGISTRY

    findings = check_core_version_compat()
    names = {f.name for f in findings}
    for provider in PROVIDER_REGISTRY:
        assert f"{provider}-compat" in names
        assert f"{provider}-store" in names


def test_check_core_version_compat_is_not_in_run_all_checks_default_set():
    source = inspect.getsource(run_all_checks)
    assert "check_core_version_compat" not in source
