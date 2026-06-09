from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agent_takkub import limit_status


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_fetch_usage_returns_three_windows_from_config_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "other-user" / ".claude"
    config_dir.mkdir(parents=True)
    expires_at_ms = int(datetime(2099, 1, 1, tzinfo=UTC).timestamp() * 1000)
    (config_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "override-token",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "five_hour": {"utilization": 12.5, "resets_at": "2026-06-09T10:00:00+00:00"},
        "seven_day": {"utilization": 34, "resets_at": "2026-06-15T10:00:00+00:00"},
        "seven_day_sonnet": {
            "utilization": 56.75,
            "resets_at": "2026-06-16T10:00:00+00:00",
        },
        "extra_usage": {"is_enabled": True},
    }
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _FakeResponse(payload)

    monkeypatch.setattr(limit_status.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(limit_status, "_resolve_user_agent", lambda: "test-agent")

    usage = limit_status.fetch_usage(config_dir)

    assert usage is not None
    assert usage.plan == "Max 20x"
    assert usage.extra_usage_enabled is True
    assert [(window.name, window.utilization) for window in usage.windows] == [
        ("five_hour", 12.5),
        ("seven_day", 34.0),
        ("seven_day_sonnet", 56.75),
    ]
    assert [window.resets_at.isoformat() for window in usage.windows] == [
        "2026-06-09T10:00:00+00:00",
        "2026-06-15T10:00:00+00:00",
        "2026-06-16T10:00:00+00:00",
    ]
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == limit_status._USAGE_URL
    assert request.get_header("Authorization") == "Bearer override-token"
    assert timeout == limit_status._TIMEOUT_S
