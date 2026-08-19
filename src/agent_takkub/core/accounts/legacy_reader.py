"""LegacyReader: `~/.takkub/user-profiles.json` (+ per-project
`user-profile.json`) -> `ProviderAccount` rows (plan §5.4: "ทุกไฟล์ V1 มี
LegacyReader ที่แปลงเป็น V2 domain object ตอนอ่าน").

Task scope: "LegacyReader อ่าน ~/.takkub/user-profiles.json +
projects/<slug>/user-profile.json -> ProviderAccount แถวแรก (claude)" —
`user_profile.py` is claude-only IN PRACTICE today even though its schema
already carries a `provider` field (`profiles_for_provider` exists but
nothing currently registers a non-claude profile). This reader converts
whatever `user_profile.list_profiles()` actually returns rather than
hardcoding `provider="claude"`, so it picks up a future non-claude profile
for free — but it is only PROVEN against claude accounts this phase (see
docs/v2/phase3-report.md).

`user_profile.py` itself is untouched — this module only READS it, exactly
like `core.storage.legacy_reader`'s existing `read_role_provider_routing_policy`
reads `role-providers.json`.
"""

from __future__ import annotations

from agent_takkub.core.models.account import AccountStatus, ProviderAccount


def _account_id(provider: str, name: str) -> str:
    return f"legacy-{provider}-{name}"


def read_legacy_accounts(project: str | None = None) -> list[ProviderAccount]:
    """Every registered profile (implicit ``default`` + user-added), each as
    one `ProviderAccount`. `secret_ref` points at the credential the SAME
    provider CLI already reads from (`secret://provider/account-id`,
    `core.secrets.manager`'s scheme) — never a raw credential.
    """
    from agent_takkub import user_profile

    accounts: list[ProviderAccount] = []
    for entry in user_profile.list_profiles():
        provider = user_profile.normalize_provider(entry.get("provider", "claude"))
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        accounts.append(
            ProviderAccount(
                id=_account_id(provider, name),
                provider_id=provider,
                label=name,
                secret_ref=f"secret://{provider}/{name}",
                status=AccountStatus.ACTIVE,
                priority=0 if name == user_profile.DEFAULT_PROFILE else 10,
                project_id=project,
            )
        )
    return accounts


def read_selected_account_id(project: str, provider: str = "claude") -> str:
    """This reader's account id for whichever legacy profile is currently
    selected for *project*/*provider* (`user_profile.profile_for`) — the
    account an `AccountPool` should default to for a project that has not
    been migrated onto real V2 pool config yet."""
    from agent_takkub import user_profile

    name = user_profile.profile_for(project, provider)
    return _account_id(user_profile.normalize_provider(provider), name)
