"""core.context_sources — issue #372 (OpenViking optional sidecar + Context
Sources + hybrid merge): base vocabulary, the adapter's fail-open HTTP
client, each `ContextSource`, and the resource-indexing bookkeeping."""

from __future__ import annotations

import json

import pytest

from agent_takkub.core.context_sources import openviking_adapter
from agent_takkub.core.context_sources.base import ContextItem, collapse_near_duplicates

# ── base.py ──────────────────────────────────────────────────────────────


def _item(
    text: str, *, source: str = "resource", trust: str = "curated", score: float = 0.0
) -> ContextItem:
    return ContextItem(
        text=text,
        tokens=max(1, len(text) // 4),
        source=source,
        provenance="p",
        trust=trust,
        score=score,
    )


def test_collapse_near_duplicates_keeps_the_longer_of_a_restated_pair():
    a = _item("the deploy pipeline uses github actions for ci")
    b = _item("deploy pipeline uses github actions for ci and cd both")
    kept, dropped = collapse_near_duplicates([a, b])
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].text == b.text


def test_collapse_near_duplicates_keeps_distinct_items():
    a = _item("rebuild and restart admin and frontend, both healthy again")
    b = _item("rebuild and restart the api container, health check green")
    kept, dropped = collapse_near_duplicates([a, b])
    assert dropped == 0
    assert len(kept) == 2


def test_collapse_near_duplicates_empty_text_never_merges():
    a = _item("")
    b = _item("")
    kept, dropped = collapse_near_duplicates([a, b])
    assert dropped == 0
    assert len(kept) == 2


# ── openviking_adapter: flags ────────────────────────────────────────────


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TAKKUB_OPENVIKING_ENABLED", raising=False)
    assert openviking_adapter.enabled() is False


def test_enabled_requires_exact_string_one(monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "true")
    assert openviking_adapter.enabled() is False
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    assert openviking_adapter.enabled() is True


def test_mode_defaults_to_shadow_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("TAKKUB_OPENVIKING_MODE", raising=False)
    assert openviking_adapter.mode() == "shadow"
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "bogus")
    assert openviking_adapter.mode() == "shadow"
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "HYBRID")
    assert openviking_adapter.mode() == "hybrid"


def test_base_url_default_and_override(monkeypatch):
    monkeypatch.delenv("TAKKUB_OPENVIKING_URL", raising=False)
    assert openviking_adapter.base_url() == "http://127.0.0.1:1933"
    monkeypatch.setenv("TAKKUB_OPENVIKING_URL", "http://example.local:9/")
    assert openviking_adapter.base_url() == "http://example.local:9"


def test_api_key_env_wins_over_file(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "DATA_HOME", tmp_path)
    key_file = tmp_path / "openviking" / "api_key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("file-key", encoding="utf-8")

    monkeypatch.setenv("TAKKUB_OPENVIKING_API_KEY", "env-key")
    assert openviking_adapter.api_key() == "env-key"

    monkeypatch.delenv("TAKKUB_OPENVIKING_API_KEY", raising=False)
    assert openviking_adapter.api_key() == "file-key"


def test_api_key_missing_everywhere_returns_none(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "DATA_HOME", tmp_path)
    monkeypatch.delenv("TAKKUB_OPENVIKING_API_KEY", raising=False)
    assert openviking_adapter.api_key() is None


# ── openviking_adapter: HTTP round-trip (fake transport) ────────────────


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


@pytest.fixture
def ov_on(monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_URL", "http://sidecar.local:1933")
    monkeypatch.delenv("TAKKUB_OPENVIKING_API_KEY", raising=False)


def test_health_ok_and_known_version(ov_on, monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url == "http://sidecar.local:1933/health"
        return _FakeResponse({"status": "ok", "healthy": True, "version": "0.4.12"})

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", fake_urlopen)
    status = openviking_adapter.health()
    assert status.ok is True
    assert status.healthy is True
    assert status.version == "0.4.12"
    assert status.known_version is True


def test_health_flags_unknown_version(ov_on, monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"healthy": True, "version": "9.9.9"})

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", fake_urlopen)
    status = openviking_adapter.health()
    assert status.ok is True
    assert status.known_version is False


def test_health_unreachable_fails_open(ov_on, monkeypatch):
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", fake_urlopen)
    status = openviking_adapter.health()
    assert status.ok is False
    assert status.error == "unreachable"


def test_health_timeout_fails_open(ov_on, monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", fake_urlopen)
    status = openviking_adapter.health()
    assert status.ok is False


def test_health_malformed_json_fails_open(ov_on, monkeypatch):
    class BadResponse(_FakeResponse):
        def read(self):
            return b"not json"

    monkeypatch.setattr(
        openviking_adapter.urllib.request, "urlopen", lambda *a, **kw: BadResponse({})
    )
    status = openviking_adapter.health()
    assert status.ok is False


def test_search_resources_disabled_returns_empty_without_network(monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "0")

    def boom(*a, **kw):
        raise AssertionError("must not touch the network when disabled")

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", boom)
    assert openviking_adapter.search_resources("anything") == []


def test_search_resources_parses_matched_context_shape(ov_on, monkeypatch):
    payload = {
        "status": "ok",
        "result": {
            "resources": [
                {
                    "uri": "viking://res/1",
                    "overview": "how deploys work here",
                    "score": 0.9,
                    "category": "resource",
                },
                {
                    "uri": "viking://res/2",
                    "abstract": "fallback to abstract when overview missing",
                    "score": 0.4,
                },
            ],
            "total": 2,
        },
    }
    monkeypatch.setattr(
        openviking_adapter.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(payload)
    )
    hits = openviking_adapter.search_resources("deploy")
    assert [h.uri for h in hits] == ["viking://res/1", "viking://res/2"]
    assert hits[1].text == "fallback to abstract when overview missing"
    assert hits[0].score == 0.9


def test_search_resources_schema_drift_returns_empty(ov_on, monkeypatch):
    monkeypatch.setattr(
        openviking_adapter.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse({"result": "not-a-dict"}),
    )
    assert openviking_adapter.search_resources("anything") == []


def test_search_resources_skips_items_missing_uri_or_text(ov_on, monkeypatch):
    payload = {
        "result": {
            "resources": [{"uri": "", "overview": "x"}, {"uri": "viking://ok", "overview": ""}]
        }
    }
    monkeypatch.setattr(
        openviking_adapter.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(payload)
    )
    assert openviking_adapter.search_resources("q") == []


def test_add_resource_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "0")

    def boom(*a, **kw):
        raise AssertionError("must not touch the network when disabled")

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", boom)
    from pathlib import Path

    assert openviking_adapter.add_resource(Path("x.md")) is None


def test_add_resource_success_returns_confirmed_root_uri(ov_on, monkeypatch, tmp_path):
    """issue #377: the real API's `POST /api/v1/resources` response carries
    `result.root_uri` — the resource's actual identity, which callers must
    key off instead of assuming their own `to=` request was honored."""
    monkeypatch.setattr(
        openviking_adapter.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse({"result": {"status": "success", "root_uri": "viking://x"}}),
    )
    f = tmp_path / "note.md"
    f.write_text("hello", encoding="utf-8")
    assert openviking_adapter.add_resource(f, to="viking://requested") == "viking://x"


def test_add_resource_failure_response(ov_on, monkeypatch, tmp_path):
    monkeypatch.setattr(
        openviking_adapter.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse({"result": {"status": "error"}}),
    )
    f = tmp_path / "note.md"
    f.write_text("hello", encoding="utf-8")
    assert openviking_adapter.add_resource(f) is None


def test_add_resource_success_without_root_uri_is_failure(ov_on, monkeypatch, tmp_path):
    """Schema-drift guard: a "success" status with no `root_uri` field is
    treated as a failure — this module never invents an identity to key
    the local registry off of."""
    monkeypatch.setattr(
        openviking_adapter.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse({"result": {"status": "success"}}),
    )
    f = tmp_path / "note.md"
    f.write_text("hello", encoding="utf-8")
    assert openviking_adapter.add_resource(f) is None
