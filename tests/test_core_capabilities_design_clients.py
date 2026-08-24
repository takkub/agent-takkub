"""core.capabilities.design_clients — #373: real HTTP clients for
21st.dev/Figma/Penpot, exercised entirely via a fake `Transport` (no real
network call ever made in this suite). Covers the happy path, the
fail-open contract (timeout/HTTP-error/shape-drift all degrade to `None`,
never raise), and that every returned record carries `Provenance`."""

from __future__ import annotations

import json
import urllib.error

import pytest

from agent_takkub.core.capabilities.design_clients import (
    FigmaClient,
    PenpotClient,
    TwentyFirstClient,
)
from agent_takkub.core.resilience.circuit_breaker import CircuitBreaker, CircuitState, get_breaker


def _json_transport(payload: object):
    def transport(method, url, headers, body, timeout):
        return json.dumps(payload).encode("utf-8")

    return transport


def _raising_transport(exc: Exception):
    def transport(method, url, headers, body, timeout):
        raise exc

    return transport


def _non_json_transport():
    def transport(method, url, headers, body, timeout):
        return b"not json at all"

    return transport


class TestTwentyFirstClient:
    def test_search_without_base_url_returns_none_never_guesses(self) -> None:
        client = TwentyFirstClient(api_key="k", base_url=None)

        assert client.search("button") is None

    def test_search_happy_path_returns_provenanced_results(self) -> None:
        payload = {
            "results": [
                {
                    "id": "c1",
                    "title": "Card",
                    "url": "https://21st.dev/c1",
                    "license": "MIT",
                    "tags": ["ui"],
                }
            ]
        }
        client = TwentyFirstClient(
            api_key="k", base_url="https://example.test/api", transport=_json_transport(payload)
        )

        results = client.search("card")

        assert results is not None
        assert len(results) == 1
        r = results[0]
        assert r.id == "c1"
        assert r.title == "Card"
        assert r.tags == ("ui",)
        assert r.provenance.source == "21st.dev"
        assert r.provenance.license == "MIT"
        assert r.provenance.fetched_at

    def test_search_drops_items_missing_required_fields(self) -> None:
        payload = {"results": [{"title": "no id here"}, {"id": "ok", "title": "OK"}]}
        client = TwentyFirstClient(
            api_key="k", base_url="https://example.test/api", transport=_json_transport(payload)
        )

        results = client.search("x")

        assert results is not None
        assert [r.id for r in results] == ["ok"]

    def test_search_schema_drift_returns_none_not_raise(self) -> None:
        client = TwentyFirstClient(
            api_key="k",
            base_url="https://example.test/api",
            transport=_json_transport({"totally": "different shape"}),
        )

        assert client.search("x") is None

    def test_search_non_json_response_returns_none(self) -> None:
        client = TwentyFirstClient(
            api_key="k", base_url="https://example.test/api", transport=_non_json_transport()
        )

        assert client.search("x") is None

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("timed out"),
            urllib.error.URLError("connection refused"),
            urllib.error.HTTPError("u", 500, "boom", {}, None),
        ],
    )
    def test_transport_failure_never_raises(self, exc: Exception) -> None:
        client = TwentyFirstClient(
            api_key="k", base_url="https://example.test/api", transport=_raising_transport(exc)
        )

        assert client.search("x") is None

    def test_get_inspiration_without_base_url_returns_none(self) -> None:
        client = TwentyFirstClient(api_key="k", base_url=None)

        assert client.get_inspiration("dashboard") is None


class TestFigmaClient:
    def test_get_file_summary_happy_path(self) -> None:
        payload = {"name": "My File", "lastModified": "2026-08-24T00:00:00Z", "version": "42"}
        client = FigmaClient(token="tok", transport=_json_transport(payload))

        summary = client.get_file_summary("abc123")

        assert summary is not None
        assert summary.key == "abc123"
        assert summary.name == "My File"
        assert summary.provenance.source == "figma"

    def test_get_file_summary_shape_drift_returns_none(self) -> None:
        client = FigmaClient(token="tok", transport=_json_transport({"unexpected": True}))

        assert client.get_file_summary("abc123") is None

    def test_list_local_variables_happy_path(self) -> None:
        payload = {
            "meta": {
                "variables": {
                    "VariableID:1": {
                        "name": "color/bg",
                        "resolvedType": "COLOR",
                        "variableCollectionId": "coll1",
                    },
                    "VariableID:2": {"name": ""},
                }
            }
        }
        client = FigmaClient(token="tok", transport=_json_transport(payload))

        variables = client.list_local_variables("abc123")

        assert variables is not None
        assert len(variables) == 1
        assert variables[0].name == "color/bg"
        assert variables[0].variable_type == "COLOR"

    def test_list_local_variables_shape_drift_returns_none(self) -> None:
        client = FigmaClient(token="tok", transport=_json_transport({"meta": {}}))

        assert client.list_local_variables("abc123") is None

    def test_list_components_happy_path(self) -> None:
        payload = {"meta": {"components": [{"key": "k1", "name": "Button", "description": "d"}]}}
        client = FigmaClient(token="tok", transport=_json_transport(payload))

        components = client.list_components("abc123")

        assert components is not None
        assert components[0].key == "k1"
        assert components[0].name == "Button"

    def test_transport_failure_never_raises(self) -> None:
        client = FigmaClient(token="tok", transport=_raising_transport(TimeoutError()))

        assert client.get_file_summary("abc123") is None


class TestPenpotClient:
    def test_get_profile_happy_path(self) -> None:
        payload = {"id": "u1", "fullname": "Ada Lovelace", "email": "ada@example.test"}
        client = PenpotClient(
            base_url="https://design.example.test", token="tok", transport=_json_transport(payload)
        )

        profile = client.get_profile()

        assert profile is not None
        assert profile.fullname == "Ada Lovelace"
        assert profile.provenance.source == "penpot"

    def test_get_profile_shape_drift_returns_none(self) -> None:
        client = PenpotClient(
            base_url="https://design.example.test",
            token="tok",
            transport=_json_transport({"nope": True}),
        )

        assert client.get_profile() is None

    def test_get_file_happy_path(self) -> None:
        payload = {
            "id": "f1",
            "name": "Design File",
            "project-id": "p1",
            "modified-at": "2026-08-24",
        }
        client = PenpotClient(
            base_url="https://design.example.test", token="tok", transport=_json_transport(payload)
        )

        summary = client.get_file("f1")

        assert summary is not None
        assert summary.name == "Design File"
        assert summary.project_id == "p1"

    def test_transport_failure_never_raises(self) -> None:
        client = PenpotClient(
            base_url="https://design.example.test",
            token="tok",
            transport=_raising_transport(urllib.error.URLError("down")),
        )

        assert client.get_profile() is None

    def test_authorization_header_uses_token_scheme(self) -> None:
        seen: dict = {}

        def transport(method, url, headers, body, timeout):
            seen["headers"] = headers
            seen["method"] = method
            return json.dumps({"id": "u1", "fullname": "x", "email": "x@x"}).encode("utf-8")

        client = PenpotClient(
            base_url="https://design.example.test", token="secret-tok", transport=transport
        )
        client.get_profile()

        assert seen["headers"]["Authorization"] == "Token secret-tok"


@pytest.fixture(autouse=True)
def _isolated_breaker_state(tmp_path, monkeypatch):
    """`CircuitBreaker.record_success`/`record_failure` best-effort-persist
    to `DATA_HOME/resilience/state.json` on every transition — redirect
    `DATA_HOME` so `TestCircuitBreakerWiring` never writes into this repo's
    own working tree (this module's own dev checkout has no
    `AGENT_TAKKUB_HOME` set, so unpatched `DATA_HOME` resolves to the repo
    root itself)."""
    from agent_takkub import config

    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")


class _CountingTransport:
    """Fake transport that counts invocations and raises on demand — used to
    prove a breaker actually skips the real call, not just returns `None`
    for some other reason."""

    def __init__(self, *, fail: bool = True, payload: bytes = b"") -> None:
        self.calls = 0
        self._fail = fail
        self._payload = payload

    def __call__(self, method, url, headers, body, timeout):
        self.calls += 1
        if self._fail:
            raise TimeoutError("simulated down")
        return self._payload


class TestCircuitBreakerWiring:
    """design_clients — v2-hardening D/F: every client call routes through
    `core.resilience.circuit_breaker`. Each test injects its own `breaker=`
    so it never touches the shared process-wide registry other tests (or
    other modules) might also be using."""

    def test_repeated_failures_open_breaker_then_transport_stops_being_called(self) -> None:
        breaker = CircuitBreaker("test-21st", failure_threshold=3, cooldown_s=60.0)
        transport = _CountingTransport(fail=True)
        client = TwentyFirstClient(
            api_key="k", base_url="https://example.test/api", transport=transport, breaker=breaker
        )

        for _ in range(3):
            assert client.search("x") is None
        assert transport.calls == 3
        assert breaker.snapshot().state == CircuitState.OPEN

        # Circuit now open — further calls must not reach the transport at all.
        for _ in range(5):
            assert client.search("x") is None
        assert transport.calls == 3

    def test_success_keeps_breaker_closed_across_many_calls(self) -> None:
        breaker = CircuitBreaker("test-figma", failure_threshold=3, cooldown_s=60.0)
        payload = json.dumps({"name": "f", "lastModified": None, "version": None}).encode("utf-8")
        transport = _CountingTransport(fail=False, payload=payload)
        client = FigmaClient(token="tok", transport=transport, breaker=breaker)

        for _ in range(10):
            assert client.get_file_summary("abc") is not None
        assert transport.calls == 10
        assert breaker.snapshot().state == CircuitState.CLOSED

    def test_half_open_probe_recovers_penpot_client_after_cooldown(self) -> None:
        class _Clock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = _Clock()
        breaker = CircuitBreaker("test-penpot", failure_threshold=1, cooldown_s=30.0, clock=clock)
        failing = _CountingTransport(fail=True)
        client = PenpotClient(
            base_url="https://design.example.test", token="tok", transport=failing, breaker=breaker
        )

        assert client.get_profile() is None  # one failure trips the breaker
        assert breaker.snapshot().state == CircuitState.OPEN

        clock.now = 30.0
        payload = json.dumps({"id": "u1", "fullname": "x", "email": "x@x"}).encode("utf-8")
        client._transport = _CountingTransport(fail=False, payload=payload)  # service recovered

        profile = client.get_profile()  # the half-open probe

        assert profile is not None
        assert breaker.snapshot().state == CircuitState.CLOSED

    def test_default_breaker_is_shared_per_source_when_none_injected(self) -> None:
        client = FigmaClient(token="tok")
        assert client._breaker is get_breaker(FigmaClient.SOURCE)
