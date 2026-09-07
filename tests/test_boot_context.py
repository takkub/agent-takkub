"""boot_context.py — issue #516 point 1 measurement engine."""

from __future__ import annotations

from agent_takkub import boot_context


def test_estimate_tokens_never_zero_for_nonempty_text():
    assert boot_context.estimate_tokens("x") == 1
    assert boot_context.estimate_tokens("") == 1
    assert boot_context.estimate_tokens("x" * 400) == 100


def test_role_boot_report_totals_sum_categories():
    report = boot_context.RoleBootReport(role="backend", project="agent-takkub")
    report.categories.append(boot_context.CategoryMeasurement("a", 40, 10))
    report.categories.append(boot_context.CategoryMeasurement("b", 80, 20))
    assert report.total_chars == 120
    assert report.total_est_tokens == 30


def test_measure_role_appendix_covers_known_categories_for_backend():
    categories = boot_context.measure_role_appendix("backend", "agent-takkub")
    names = {c.category for c in categories}
    # role_file_base and guards_fixed are unconditional in spawn_engine's
    # assembly (every non-Lead role gets both); everything else is
    # role/project-conditional and may legitimately be absent.
    assert "role_file_base" in names
    assert "guards_fixed" in names
    for c in categories:
        assert c.chars >= 0
        assert c.est_tokens >= 1


def test_measure_role_appendix_unknown_role_does_not_raise():
    # agent_role_dir() falls back gracefully for a role with no dedicated
    # .claude/agents/<role>.md — this must degrade, never throw, since
    # doctor.check_boot_context calls it directly without its own try/except
    # per-category (only per-role, in the caller).
    categories = boot_context.measure_role_appendix("totally-unknown-role-xyz", "agent-takkub")
    assert isinstance(categories, list)


def test_measure_mcp_config_reports_no_config_gracefully():
    m = boot_context.measure_mcp_config("totally-unknown-role-xyz", "agent-takkub")
    assert m.category == "mcp_config"
    assert m.chars == 0 and m.est_tokens == 0


def test_build_report_backend_matches_sum_of_its_own_measure_calls():
    report = boot_context.build_report("backend", "agent-takkub")
    assert report.role == "backend"
    assert report.total_est_tokens == sum(c.est_tokens for c in report.categories)
    assert report.total_est_tokens > 0


def test_doctor_check_boot_context_scoped_to_one_role():
    from agent_takkub.doctor import Finding, Status, check_boot_context

    findings, report_text = check_boot_context(role="backend", project="agent-takkub")
    assert findings, "expected at least the backend Finding + any shared-memory Finding"
    assert all(isinstance(f, Finding) for f in findings)
    role_findings = [f for f in findings if f.category == "boot-context" and f.name == "backend"]
    assert len(role_findings) == 1
    assert role_findings[0].status == Status.INFO
    assert "backend" in report_text


def test_doctor_check_boot_context_warns_when_learned_notes_exceeds_live_cap(
    monkeypatch,
) -> None:
    """#516b: role_memory's own curation should make this unreachable in
    practice (it self-trims on every read) — this test fabricates the
    oversized-report case directly so the doctor-side WARN wiring itself
    is covered even though the normal pipeline is self-correcting."""
    from agent_takkub import boot_context, role_memory
    from agent_takkub.doctor import Status, check_boot_context

    oversized = boot_context.CategoryMeasurement(
        "learned_notes", role_memory._MEM_MAX_LIVE_CHARS + 500, 999, "fake/path.md"
    )

    def _fake_build_report(base_role, project_ns):
        rep = boot_context.RoleBootReport(role=base_role, project=project_ns)
        rep.categories.append(oversized)
        return rep

    monkeypatch.setattr(boot_context, "build_report", _fake_build_report)
    findings, _ = check_boot_context(role="backend", project="agent-takkub")
    warn = [f for f in findings if f.name == "backend.learned_notes"]
    assert len(warn) == 1
    assert warn[0].status == Status.WARN


def test_doctor_check_boot_context_not_in_run_all_checks_default_set():
    import inspect

    from agent_takkub.doctor import run_all_checks

    assert "check_boot_context" not in inspect.getsource(run_all_checks)
