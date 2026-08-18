"""#293: codex rollout discovery must not scan the whole session store.

`_resolve_codex_jsonl_path` runs on the Qt main thread every
`_UUIDLESS_RESYNC_THROTTLE_S` seconds — codex has `requires_session_uuid=False`,
so the #229 "skip the resolve once a tail exists" shortcut can never apply to
it. The original implementation stat-sorted every `rollout-*.jsonl` in the store
before looking at one of them: 2716 ms against 813 files on the reference box,
i.e. `Lead = codex` froze the cockpit for ~2.7 s every 5 s.

Codex partitions the store as `sessions/YYYY/MM/DD/`, so these tests pin the
two properties that make the walk cheap and still correct: newest-day-first
lazy order, and a date-bounded stop when a spawn timestamp is supplied.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agent_takkub.remote.notify import _codex_day_dirs, _codex_rollout_candidates


def _rollout(root: Path, stamp: datetime, name: str) -> Path:
    day = root / f"{stamp.year:04d}" / f"{stamp.month:02d}" / f"{stamp.day:02d}"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{name}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_day_dirs_are_newest_first(tmp_path: Path) -> None:
    _rollout(tmp_path, datetime(2026, 8, 1), "a")
    _rollout(tmp_path, datetime(2026, 8, 18), "b")
    _rollout(tmp_path, datetime(2025, 12, 31), "c")

    stamps = [stamp for stamp, _dir in _codex_day_dirs(tmp_path)]

    assert stamps == sorted(stamps, reverse=True)
    assert stamps[0] == datetime(2026, 8, 18).date()


def test_candidates_yield_newest_day_first(tmp_path: Path) -> None:
    _rollout(tmp_path, datetime(2026, 8, 1), "old")
    newest = _rollout(tmp_path, datetime(2026, 8, 18), "new")

    assert next(_codex_rollout_candidates(tmp_path)) == newest


def test_candidates_are_lazy(tmp_path: Path) -> None:
    """The first candidate must not cost a walk of the whole store — that
    laziness is the entire point, since every caller returns on first match."""
    for day in range(1, 29):
        _rollout(tmp_path, datetime(2026, 7, day), f"f{day}")
    newest = _rollout(tmp_path, datetime(2026, 8, 18), "target")

    visited: list[Path] = []
    for candidate in _codex_rollout_candidates(tmp_path):
        visited.append(candidate)
        break

    assert visited == [newest]


def test_not_before_stops_at_the_date_boundary(tmp_path: Path) -> None:
    now = datetime.now()
    recent = _rollout(tmp_path, now, "recent")
    _rollout(tmp_path, now - timedelta(days=30), "ancient")

    found = list(_codex_rollout_candidates(tmp_path, not_before=now.timestamp()))

    assert recent in found
    assert not any(p.name.endswith("ancient.jsonl") for p in found)


def test_not_before_keeps_a_day_of_slack(tmp_path: Path) -> None:
    """A session started just before midnight lands in the previous day's
    directory; a hard same-day cutoff would lose it."""
    now = datetime.now()
    yesterday = _rollout(tmp_path, now - timedelta(days=1), "just-before-midnight")

    found = list(_codex_rollout_candidates(tmp_path, not_before=now.timestamp()))

    assert yesterday in found


def test_unknown_layout_falls_back_to_whole_tree(tmp_path: Path) -> None:
    """A store that isn't date-partitioned must still be searched, not
    silently reported as empty."""
    flat = tmp_path / "misc"
    flat.mkdir()
    stray = flat / "rollout-flat.jsonl"
    stray.write_text("{}\n", encoding="utf-8")

    assert list(_codex_rollout_candidates(tmp_path)) == [stray]


def test_missing_store_yields_nothing(tmp_path: Path) -> None:
    assert list(_codex_rollout_candidates(tmp_path / "nope")) == []
