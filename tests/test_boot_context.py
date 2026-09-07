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


def test_doctor_check_boot_context_not_in_run_all_checks_default_set():
    import inspect

    from agent_takkub.doctor import run_all_checks

    assert "check_boot_context" not in inspect.getsource(run_all_checks)
