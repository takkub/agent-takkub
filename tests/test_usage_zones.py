"""Usage meter zones (2026-10-08): every account of a provider is listed
together, and the PWA's /api/usage carries the same rows as the desktop meter
(it used to send one provider-level row each — a second Codex account never
reached the phone)."""

from __future__ import annotations

from agent_takkub import provider_usage
from agent_takkub.provider_usage import ProviderUsage


class _Store:
    def __init__(self, cache, accounts):
        self._cache, self._accounts = cache, accounts

    def get_all(self):
        return dict(self._cache)

    def get_all_account_usages(self):
        return list(self._accounts)


def _two_codex_store():
    return _Store(
        {"gemini": ProviderUsage(provider="gemini", status="active")},
        [
            ProviderUsage(provider="codex", status="active", account="a@x", utilization=79),
            ProviderUsage(provider="gemini", status="active"),
            ProviderUsage(provider="codex", status="active", account="b@x", utilization=16),
        ],
    )


def test_display_rows_group_every_account_in_provider_order() -> None:
    rows = provider_usage.display_rows(_two_codex_store())
    assert [r.provider for r in rows] == [
        "claude",
        "codex",
        "codex",
        "gemini",
        "opencode",
        "cursor",
    ]
    assert [r.account for r in rows if r.provider == "codex"] == ["a@x", "b@x"]


def test_remote_usage_endpoint_sends_every_account(monkeypatch) -> None:
    from agent_takkub.remote import api

    monkeypatch.setattr(provider_usage, "get_store", _two_codex_store)
    body = api.usage()
    codex = [p for p in body["providers"] if p["provider"] == "codex"]
    assert [p["account"] for p in codex] == ["a@x", "b@x"]


def test_popup_zones_keep_accounts_together() -> None:
    from agent_takkub.usage_meter import _group_by_provider

    rows = [
        ProviderUsage(provider="codex", status="active", account="a@x"),
        ProviderUsage(provider="claude", status="active"),
        ProviderUsage(provider="codex", status="active", account="b@x"),
    ]
    zones = _group_by_provider(rows)
    assert [(p, [r.account for r in rs]) for p, rs in zones] == [
        ("codex", ["a@x", "b@x"]),
        ("claude", [None]),
    ]
