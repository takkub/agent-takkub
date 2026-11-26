"""How many workers one pane's build/test may use on THIS machine (#1064).

Test runners and bundlers default to "every core". One pane doing that is
fine; several — across projects, which no Lead can see — took a 16-thread /
40 GB machine to 100% CPU and RAM and the cockpit down with it.

Nothing here is a fixed number: the budget is a share of the machine's own
cores and memory, split by how many heavy panes the chosen machine mode
(Settings → ทั่วไป → โหมดเครื่อง) lets run at once.
"""

from __future__ import annotations

_MODE_SHARE = {"safe": 0.25, "balanced": 0.5, "maximum": 0.75}
# Rough resident size of one jest/vitest/tsc worker on a mid-size TS project
# (measured 2026-11-26: 15 jest workers at 1.5-2.2 GB each).
_GB_PER_WORKER = 2.0

# Tools that read their worker count from the environment. Set only when the
# user has not set them. (jest and `next build` have no such variable — they
# need the flag; see `worker_flag_hint`.)
_WORKER_ENV_VARS = (
    "VITEST_MAX_THREADS",
    "VITEST_MAX_FORKS",
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
    "CARGO_BUILD_JOBS",
)


def worker_budget(
    *,
    mode: str | None = None,
    logical_cpus: int | None = None,
    total_memory_gb: float | None = None,
    heavy_slots: int | None = None,
) -> int:
    """Workers one pane may start, >= 1. Arguments default to this machine
    and its saved machine mode; pass them to compute for another shape."""
    if mode is None or logical_cpus is None or total_memory_gb is None or heavy_slots is None:
        try:
            import psutil

            from . import performance_settings

            settings = performance_settings.load()
            mode = mode or settings.mode
            heavy_slots = heavy_slots or settings.max_heavy_global
            logical_cpus = logical_cpus or psutil.cpu_count(logical=True) or 4
            if total_memory_gb is None:
                total_memory_gb = psutil.virtual_memory().total / (1024**3)
        except Exception:
            mode = mode or "balanced"
            heavy_slots = heavy_slots or 2
            logical_cpus = logical_cpus or 4
            total_memory_gb = total_memory_gb if total_memory_gb is not None else 8.0
    share = _MODE_SHARE.get(str(mode).lower(), 0.5)
    slots = max(1, int(heavy_slots))
    by_cpu = int(logical_cpus * share) // slots
    by_ram = int(total_memory_gb * share / slots / _GB_PER_WORKER)
    return max(1, min(by_cpu, by_ram))


def apply_worker_budget_env(env: dict[str, str]) -> None:
    """Put the budget where a pane's tools can see it — every provider, both
    OSes. Never overrides a value the user already exported."""
    try:
        budget = str(worker_budget())
    except Exception:
        return
    env.setdefault("TAKKUB_WORKER_BUDGET", budget)
    for name in _WORKER_ENV_VARS:
        env.setdefault(name, env["TAKKUB_WORKER_BUDGET"])


def worker_flag_hint(tool: str, budget: int | None = None) -> str:
    """The flag that caps *tool*'s workers at the budget, for guard messages."""
    n = budget if budget is not None else worker_budget()
    if tool.startswith("jest"):
        return f"--maxWorkers={n}"
    if tool.startswith("vitest"):
        return f"--maxWorkers={n}"
    if tool.startswith("playwright"):
        return f"--workers={n}"
    if tool.startswith("pytest"):
        return f"-n {n}"
    return ""
