"""#725: kimi was removed from PROVIDER_REGISTRY. Stored settings that still
name it must fall back cleanly to the default provider at boot (no crash, no
dead pane), user files are rewritten in place — never deleted — and `assign`
gives a clear message instead of a traceback."""

from __future__ import annotations

import json

import pytest

from agent_takkub import provider_config, removed_providers, role_models
from agent_takkub.core.storage.v2_target import read_data, write_data
from agent_takkub.provider_spec import PROVIDER_REGISTRY

# conftest's autouse `_isolate_runtime` points `storage_layout_v2()`'s no-arg
# default at a per-test tmp dir, so `role_models.path()` and
# `provider_config._routing_target()` both resolve there.


def _plant_stale_settings() -> None:
    write_data(
        role_models.path(),
        {
            "qa": {"provider": "kimi", "model": "k2.5", "effort": "high"},
            "kimi": {"provider": "kimi"},
            "backend": {"provider": "codex", "model": "gpt-5.6"},
        },
    )
    write_data(
        role_models.projects_path(),
        {"proj": {"frontend": {"provider": "kimi", "model": "k3"}}},
    )
    target = provider_config._routing_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": 1,
                "global": {"qa": "kimi", "backend": "codex"},
                "projects": {"proj": {"frontend": "kimi", "docs": "gemini"}},
            }
        ),
        encoding="utf-8",
    )


def test_kimi_is_gone_from_the_registry() -> None:
    assert "kimi" not in PROVIDER_REGISTRY
    assert "kimi" not in provider_config.VALID_PROVIDERS


def test_stale_kimi_settings_resolve_to_default_even_before_migration() -> None:
    # The runtime already degrades: unknown providers are ignored on read.
    _plant_stale_settings()
    assert provider_config.provider_for("qa") == "claude"
    assert provider_config.provider_for("frontend", "proj") == "claude"
    assert provider_config.provider_for("backend") == "codex"
    assert role_models.model_for("qa", "claude") is None


def test_migration_rewrites_values_and_keeps_other_entries() -> None:
    _plant_stale_settings()

    changed = removed_providers.migrate_removed_provider_settings()

    assert changed == {"role_models": 3, "routing": 2}
    assert read_data(role_models.path()) == {
        "qa": {"provider": "claude"},  # model/effort were kimi ids — dropped
        "backend": {"provider": "codex", "model": "gpt-5.6"},
    }
    assert read_data(role_models.projects_path()) == {"proj": {"frontend": {"provider": "claude"}}}
    routing = json.loads(provider_config._routing_target().read_text(encoding="utf-8"))
    assert routing["global"] == {"qa": "claude", "backend": "codex"}
    assert routing["projects"] == {"proj": {"frontend": "claude", "docs": "gemini"}}
    assert provider_config.provider_for("qa") == "claude"


def test_migration_is_idempotent_and_touches_nothing_when_clean() -> None:
    _plant_stale_settings()
    removed_providers.migrate_removed_provider_settings()
    target = provider_config._routing_target()
    before = target.read_bytes()

    assert removed_providers.migrate_removed_provider_settings() == {
        "role_models": 0,
        "routing": 0,
    }
    assert target.read_bytes() == before


def test_migration_with_no_settings_files_is_a_noop() -> None:
    assert removed_providers.migrate_removed_provider_settings() == {
        "role_models": 0,
        "routing": 0,
    }
    assert not provider_config._routing_target().exists()


def test_migration_never_raises_on_a_corrupt_store() -> None:
    target = provider_config._routing_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    role_models.path().parent.mkdir(parents=True, exist_ok=True)
    role_models.path().write_text("[]", encoding="utf-8")

    result = removed_providers.migrate_removed_provider_settings()

    assert all(v >= 0 for v in result.values())
    assert target.read_text(encoding="utf-8") == "{not json"  # left as-is


def test_assign_provider_kimi_gives_a_clear_message() -> None:
    msg = provider_config.assign_provider_override_error("kimi")
    assert msg is not None
    assert "ถอด" in msg and "kimi" in msg
    assert "not a known provider" not in msg
    # a never-existed provider keeps the generic message
    assert "not a known provider" in (provider_config.assign_provider_override_error("nope") or "")


@pytest.mark.parametrize("role", ["kimi", "Kimi", "kimi#2"])
def test_removed_role_names_are_recognised(role: str) -> None:
    assert removed_providers.is_removed_provider(role)
    assert "kimi" in removed_providers.removed_provider_message(role)


def test_cli_assign_role_kimi_is_refused_with_the_message() -> None:
    import argparse

    from agent_takkub import cli

    result = cli.cmd_assign(
        argparse.Namespace(role="kimi", task="do it", task_file=None, provider=None)
    )
    assert result["ok"] is False
    assert "kimi" in result["msg"]


def test_boot_builds_the_window_after_rewriting_stale_settings(monkeypatch) -> None:
    from agent_takkub import app

    _plant_stale_settings()
    seen: list[object] = []
    monkeypatch.setattr(
        app, "MainWindow", lambda: seen.append(read_data(role_models.path())) or "window"
    )

    assert app._build_main_window() == "window"
    # the window is constructed only after the rewrite — no pane sees "kimi"
    assert seen == [
        {
            "qa": {"provider": "claude"},
            "backend": {"provider": "codex", "model": "gpt-5.6"},
        }
    ]
