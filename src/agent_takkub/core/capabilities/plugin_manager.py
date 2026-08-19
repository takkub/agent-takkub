"""PluginManager (Phase 5b, epic #309 Capability Hub).

Matrix §3 "Plugin": WRAP — "ติดตั้งผ่าน `claude plugin` CLI = claude-only
· ห่อด้วย `PluginManager` ที่มี claude backend เป็นตัวแรก แล้วประกาศ gap
สำหรับ provider อื่น (#103)". `plugin_installer.py`'s logic is untouched;
this class is a provider-keyed front door onto it, with every non-claude
provider explicit in `NO_BACKEND_PROVIDERS` rather than silently falling
through to claude's behaviour or silently doing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_takkub import plugin_installer

# No provider here has a scriptable plugin-install CLI of its own the
# cockpit can drive today — flagged explicitly per #103 ("claude-only
# shortcut ที่เลี่ยงไม่ได้ต้อง flag เข้า #103 ห้ามเงียบ"), not silently
# left unsupported. `gemini`/`agy` DO have a *bridge* for MCP-only plugin
# content (see `mcp_bridge`'s `"plugin_import"` variant docstring) but no
# general install/uninstall surface, so it stays listed here too.
NO_BACKEND_PROVIDERS: frozenset[str] = frozenset({"codex", "gemini", "opencode", "kimi", "cursor"})


class PluginBackendGapError(RuntimeError):
    """Raised by `install`/`uninstall` for a provider in
    `NO_BACKEND_PROVIDERS` — there is no installer to call, and pretending
    otherwise would silently no-op instead of telling the caller why."""


@dataclass(frozen=True, slots=True)
class PluginStatus:
    key: str
    name: str
    installed: bool


class PluginManager:
    """`claude` backend (`plugin_installer.py`, unchanged) is the only
    registered installer today; every other provider raises
    `PluginBackendGapError` naming the gap instead of pretending to have
    installed something."""

    def list_recommended(self) -> tuple[PluginStatus, ...]:
        """The cockpit's curated plugin set, each flagged installed/not —
        claude-only (`plugin_installer.RECOMMENDED` has no per-provider
        axis; every recommended plugin targets the claude backend)."""
        installed = plugin_installer.installed_on_disk()
        return tuple(
            PluginStatus(key=p.key, name=p.key, installed=p.key in installed)
            for p in plugin_installer.RECOMMENDED
        )

    def install(self, provider: str, plugin_id: str) -> tuple[bool, str]:
        if provider != "claude":
            raise PluginBackendGapError(
                f"no plugin installer for provider {provider!r} (#103 gap — "
                f"see NO_BACKEND_PROVIDERS)"
            )
        return plugin_installer.install_by_id(plugin_id)

    def uninstall(self, provider: str, plugin_id: str) -> tuple[bool, str]:
        if provider != "claude":
            raise PluginBackendGapError(
                f"no plugin installer for provider {provider!r} (#103 gap — "
                f"see NO_BACKEND_PROVIDERS)"
            )
        return plugin_installer.uninstall_plugin(plugin_id)
