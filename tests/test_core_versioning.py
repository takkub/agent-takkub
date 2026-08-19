"""Core V2 versioning: version.json store, compatibility matrix, provider
version detection, and live-store schema-drift probing (#309 Phase 4)."""

from __future__ import annotations

import json
import sqlite3

from agent_takkub.core.models.version import CompatibilityRule
from agent_takkub.core.versioning import compatibility, detector, probe, store

# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------


def test_read_version_doc_missing_file_is_empty(tmp_path):
    assert store.read_version_doc(tmp_path / "missing.json") == []


def test_read_version_doc_corrupt_file_is_empty(tmp_path):
    bad = tmp_path / "version.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert store.read_version_doc(bad) == []


def test_write_and_read_version_doc_round_trip(tmp_path):
    path = tmp_path / "version.json"
    cv = store.record_component("app", "1.2.3", path=path)
    assert cv.component == "app"
    assert cv.version == "1.2.3"

    read_back = store.read_version_doc(path)
    assert len(read_back) == 1
    assert read_back[0].component == "app"
    assert read_back[0].version == "1.2.3"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["storage_schema_version"] == store.STORAGE_SCHEMA_VERSION
    assert "app_version" in payload


def test_record_component_upserts_without_clobbering_others(tmp_path):
    path = tmp_path / "version.json"
    store.record_component("app", "1.0.0", path=path)
    store.record_component("adapter:codex", "0.147.0", path=path)
    store.record_component("app", "1.0.1", path=path)

    by_component = {c.component: c.version for c in store.read_version_doc(path)}
    assert by_component == {"app": "1.0.1", "adapter:codex": "0.147.0"}


def test_write_is_atomic_via_os_replace(tmp_path, monkeypatch):
    path = tmp_path / "version.json"
    store.write_version_doc([], path)

    calls = []
    real_replace = __import__("os").replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("agent_takkub.core.versioning.store.os.replace", spy_replace)
    store.write_version_doc([], path)
    assert len(calls) == 1
    assert not path.with_name(path.name + ".tmp").exists()


# ---------------------------------------------------------------------------
# compatibility.py
# ---------------------------------------------------------------------------


def test_claude_below_minimum_is_below_min():
    matrix = compatibility.CompatibilityMatrix()
    ev = matrix.evaluate("claude", "1.0.0")
    assert ev.verdict == compatibility.CompatVerdict.BELOW_MIN


def test_claude_meets_minimum_is_ok():
    matrix = compatibility.CompatibilityMatrix()
    ev = matrix.evaluate("claude", "99.0.0 (Claude Code)")
    assert ev.verdict == compatibility.CompatVerdict.OK


def test_unregistered_provider_is_uncalibrated():
    matrix = compatibility.CompatibilityMatrix()
    ev = matrix.evaluate("kimi", "1.0.0")
    assert ev.verdict == compatibility.CompatVerdict.UNCALIBRATED
    assert ev.rule is None


def test_unparseable_version_is_unknown():
    matrix = compatibility.CompatibilityMatrix()
    ev = matrix.evaluate("claude", "not-a-version")
    assert ev.verdict == compatibility.CompatVerdict.UNKNOWN


def test_none_version_text_is_unknown_when_calibrated():
    matrix = compatibility.CompatibilityMatrix()
    ev = matrix.evaluate("claude", None)
    assert ev.verdict == compatibility.CompatVerdict.UNKNOWN


def test_max_exclusive_boundary_is_above_max():
    rule = CompatibilityRule(id="x", component="x", min_version=None, max_version="2.0.0")
    matrix = compatibility.CompatibilityMatrix({"x": rule})
    assert matrix.evaluate("x", "2.0.0").verdict == compatibility.CompatVerdict.ABOVE_MAX
    assert matrix.evaluate("x", "1.9.9").verdict == compatibility.CompatVerdict.OK


def test_max_inclusive_boundary_is_ok():
    rule = CompatibilityRule(
        id="x", component="x", min_version=None, max_version="2.0.0", max_exclusive=False
    )
    matrix = compatibility.CompatibilityMatrix({"x": rule})
    assert matrix.evaluate("x", "2.0.0").verdict == compatibility.CompatVerdict.OK
    assert matrix.evaluate("x", "2.0.1").verdict == compatibility.CompatVerdict.ABOVE_MAX


def test_supports_feature():
    rule = CompatibilityRule(id="x", component="x", features=("resume", "mcp"))
    matrix = compatibility.CompatibilityMatrix({"x": rule})
    assert matrix.supports_feature("x", "resume") is True
    assert matrix.supports_feature("x", "unknown-feature") is False
    assert matrix.supports_feature("unregistered", "resume") is False


def test_register_adds_a_new_rule():
    matrix = compatibility.CompatibilityMatrix({})
    matrix.register(CompatibilityRule(id="codex", component="codex", min_version="0.100.0"))
    assert matrix.evaluate("codex", "0.50.0").verdict == compatibility.CompatVerdict.BELOW_MIN


# ---------------------------------------------------------------------------
# detector.py
# ---------------------------------------------------------------------------


class _FakeSpec:
    def __init__(self, name, bin_path):
        self.name = name
        self.binary_names = [name]
        self.custom_discovery_fn = None
        self._bin_path = bin_path


def test_detect_returns_none_for_unknown_provider(monkeypatch):
    monkeypatch.setattr(detector, "PROVIDER_REGISTRY", {})
    d = detector.ProviderVersionDetector()
    result = d.detect("nonexistent")
    assert result.version_text is None
    assert result.path is None


def test_detect_returns_none_when_binary_not_found(monkeypatch):
    monkeypatch.setattr(detector, "PROVIDER_REGISTRY", {"foo": _FakeSpec("foo", None)})
    monkeypatch.setattr(detector.provider_probe, "resolve_provider_bin", lambda spec: None)
    d = detector.ProviderVersionDetector()
    result = d.detect("foo")
    assert result.version_text is None
    assert result.path is None


def test_detect_parses_first_line_of_version_output(monkeypatch):
    monkeypatch.setattr(detector, "PROVIDER_REGISTRY", {"foo": _FakeSpec("foo", "/bin/foo")})
    monkeypatch.setattr(detector.provider_probe, "resolve_provider_bin", lambda spec: "/bin/foo")
    monkeypatch.setattr(
        detector.provider_probe, "run_probe", lambda argv, **kw: (0, "1.2.3 (Foo CLI)\nextra line")
    )
    d = detector.ProviderVersionDetector()
    result = d.detect("foo")
    assert result.version_text == "1.2.3 (Foo CLI)"
    assert result.path == "/bin/foo"


def test_detect_nonzero_exit_is_none_version(monkeypatch):
    monkeypatch.setattr(detector, "PROVIDER_REGISTRY", {"foo": _FakeSpec("foo", "/bin/foo")})
    monkeypatch.setattr(detector.provider_probe, "resolve_provider_bin", lambda spec: "/bin/foo")
    monkeypatch.setattr(detector.provider_probe, "run_probe", lambda argv, **kw: (1, "error"))
    d = detector.ProviderVersionDetector()
    result = d.detect("foo")
    assert result.version_text is None
    assert result.path == "/bin/foo"


def test_detect_all_covers_every_registered_provider(monkeypatch):
    monkeypatch.setattr(
        detector,
        "PROVIDER_REGISTRY",
        {"a": _FakeSpec("a", None), "b": _FakeSpec("b", None)},
    )
    monkeypatch.setattr(detector.provider_probe, "resolve_provider_bin", lambda spec: None)
    d = detector.ProviderVersionDetector()
    result = d.detect_all()
    assert set(result.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# probe.py
# ---------------------------------------------------------------------------


def test_probe_claude_no_directory_is_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.config, "default_claude_config_dir", lambda: tmp_path / "no-claude")
    result = probe._probe_claude()
    assert result.found is False


def test_probe_claude_samples_keys_from_sessions(monkeypatch, tmp_path):
    projects = tmp_path / "claude" / "projects" / "proj1"
    projects.mkdir(parents=True)
    session = projects / "session.jsonl"
    session.write_text(
        json.dumps({"type": "user", "message": "hi", "uuid": "x"}) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(probe.config, "default_claude_config_dir", lambda: tmp_path / "claude")
    result = probe._probe_claude()
    assert result.found is True
    assert result.fingerprint == frozenset({"type", "message", "uuid"})
    assert result.sampled == 1


def test_probe_codex_no_directory_is_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.codex_helper, "codex_sessions_root", lambda: tmp_path / "no-codex")
    result = probe._probe_codex()
    assert result.found is False


def test_probe_codex_samples_keys(monkeypatch, tmp_path):
    sessions = tmp_path / "codex" / "sessions" / "2026" / "08" / "19"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-1.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {}}) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        probe.codex_helper, "codex_sessions_root", lambda: tmp_path / "codex" / "sessions"
    )
    result = probe._probe_codex()
    assert result.found is True
    assert "type" in result.fingerprint


def test_probe_opencode_no_db_is_not_found(monkeypatch):
    monkeypatch.setattr(probe.opencode_helper, "opencode_db_path", lambda: None)
    result = probe._probe_opencode()
    assert result.found is False


def test_probe_opencode_reads_table_names(monkeypatch, tmp_path):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE session (id TEXT)")
    conn.execute("CREATE TABLE message (id TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(probe.opencode_helper, "opencode_db_path", lambda: db_path)
    result = probe._probe_opencode()
    assert result.found is True
    assert result.fingerprint == frozenset({"session", "message"})


def test_probe_store_unknown_provider_returns_not_found():
    result = probe.probe_store("gemini")
    assert result.found is False
    assert "no confirmed store location" in result.note


def test_probe_store_never_raises_on_resolver_error(monkeypatch):
    def _boom():
        raise RuntimeError("disk exploded")

    monkeypatch.setitem(probe._RESOLVERS, "claude", _boom)
    result = probe.probe_store("claude")
    assert result.found is False
    assert "disk exploded" in result.note


def test_detect_drift_no_baseline_is_not_drifted():
    current = probe.StoreProbeResult("claude", True, frozenset({"a", "b"}), 1)
    drift = probe.detect_drift(current, None)
    assert drift.drifted is False


def test_detect_drift_same_fingerprint_is_not_drifted():
    current = probe.StoreProbeResult("claude", True, frozenset({"a", "b"}), 1)
    drift = probe.detect_drift(current, frozenset({"a", "b"}))
    assert drift.drifted is False


def test_detect_drift_changed_fingerprint_is_drifted():
    current = probe.StoreProbeResult("codex", True, frozenset({"a", "c"}), 1)
    drift = probe.detect_drift(current, frozenset({"a", "b"}))
    assert drift.drifted is True
    assert drift.added == frozenset({"c"})
    assert drift.removed == frozenset({"b"})


def test_detect_drift_not_found_is_never_drifted():
    current = probe.StoreProbeResult("gemini", False, note="no store")
    drift = probe.detect_drift(current, frozenset({"a"}))
    assert drift.drifted is False
