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


def test_role_file_base_ignores_stale_runtime_cache(monkeypatch, tmp_path):
    """#516 follow-up: `role_file_base` reads `agent_role_dir(role)/CLAUDE.md`,
    which `config.agent_role_dir` unconditionally rewrites from `.claude/
    agents/<role>.md` on every call — never a stale materialised copy left
    behind by an older code version or a prior spawn. Seed the staging dir
    with garbage before measuring to lock that in; a regression that made
    `agent_role_dir` skip the rewrite when the file already exists would
    make this test fail on the garbage content instead."""
    from agent_takkub import config

    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime, raising=False)

    fresh = next(
        c
        for c in boot_context.measure_role_appendix("backend", "agent-takkub")
        if c.category == "role_file_base"
    )

    staging = config.agent_role_dir("backend")
    (staging / "CLAUDE.md").write_text("STALE PRE-DIET CONTENT " * 500, encoding="utf-8")

    again = next(
        c
        for c in boot_context.measure_role_appendix("backend", "agent-takkub")
        if c.category == "role_file_base"
    )
    assert again.chars == fresh.chars
    assert again.est_tokens == fresh.est_tokens


def test_graft_caveats_never_enters_repo_controlled_ceiling(monkeypatch, tmp_path):
    """#516 follow-up: whether a role gets `graft_caveats` is decided by
    `shared_dev_tools.role_mcp_allowlist`, which merges the built-in policy
    with a per-machine `SETTINGS_HOME/pane-tools.json` operator override —
    real mutable machine config, not repo content (same class as
    `learned_notes`). A `doctor --boot-context` run on a machine with a
    graft override and `test_boot_context_ceiling.py` (which always runs
    under conftest's isolated, override-free `SETTINGS_HOME`) must never
    disagree on `repo_controlled_est_tokens` because of it."""
    from agent_takkub import pane_tools_policy

    without_override = boot_context.build_report("frontend", "agent-takkub")

    policy_file = tmp_path / "pane-tools.json"
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", policy_file, raising=False)
    policy_file.write_text(
        '{"version": 1, "roles": {"frontend": {"mcps": ["graft"], "plugins": []}}}',
        encoding="utf-8",
    )

    with_override = boot_context.build_report("frontend", "agent-takkub")

    graft_cats = [c for c in with_override.categories if c.category == "graft_caveats"]
    assert graft_cats, "override should have actually granted graft for this to be a real check"
    assert with_override.repo_controlled_est_tokens == without_override.repo_controlled_est_tokens


def test_doctor_check_boot_context_not_in_run_all_checks_default_set():
    import inspect

    from agent_takkub.doctor import run_all_checks

    assert "check_boot_context" not in inspect.getsource(run_all_checks)


def _write_skill(skills_dir, name: str, description: str = "") -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
    )


class TestNativeSkillCatalogMeasurement:
    """#516 follow-up F2: the native `skill_listing` catalog (CLAUDE_CONFIG_DIR/
    skills/, distinct from cockpit's own skill_matrix_appendix) was never
    measured before — closes that gap so `takkub doctor --boot-context` can
    show it for the first time."""

    def test_no_skills_dir_returns_none(self, monkeypatch, tmp_path):
        from agent_takkub import user_profile

        # This machine's real per-project profile (registered outside any
        # test isolation, in ~/.takkub/projects/<slug>/user-profile.json)
        # can resolve to a real CLAUDE_CONFIG_DIR with real skills — patch
        # config_dir_for itself so this test is deterministic regardless of
        # the machine it runs on.
        monkeypatch.setattr(user_profile, "config_dir_for", lambda project: tmp_path)
        assert boot_context.measure_native_skill_catalog("agent-takkub") is None

    def test_counts_files_and_flags_unrelated_skills(self, monkeypatch, tmp_path):
        from agent_takkub import user_profile

        monkeypatch.setattr(user_profile, "config_dir_for", lambda project: tmp_path)
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "some-unrelated-personal-skill", "not agent-takkub")
        _write_skill(skills_dir, "another-one")

        m = boot_context.measure_native_skill_catalog("agent-takkub")

        assert m is not None
        assert m.category == "native_skill_catalog"
        assert m.chars > 0
        assert m.est_tokens >= 1
        assert "2 skill(s)" in m.detail
        assert "some-unrelated-personal-skill" in m.detail

    def test_dynamic_state_category_not_gated(self):
        assert "native_skill_catalog" in boot_context.DYNAMIC_STATE_CATEGORIES

    def test_doctor_reports_native_skill_catalog_finding(self, monkeypatch, tmp_path):
        from agent_takkub import user_profile
        from agent_takkub.doctor import check_boot_context

        monkeypatch.setattr(user_profile, "config_dir_for", lambda project: tmp_path)
        _write_skill(tmp_path / "skills", "some-other-skill")

        findings, report_text = check_boot_context(role="backend", project="agent-takkub")

        hits = [f for f in findings if f.name == "native_skill_catalog"]
        assert len(hits) == 1
        assert "no per-skill CLI gate" in hits[0].detail
        assert "native_skill_catalog" in report_text

    def test_format_report_shows_dynamic_state_tag(self):
        m = boot_context.CategoryMeasurement("native_skill_catalog", 100, 25, "fake/skills")
        text = boot_context.format_report([], None, m)
        assert "native_skill_catalog" in text
        assert "[dynamic-state, not gated]" in text
