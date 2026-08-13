"""
Generate docs/architecture/depgraph.json using grimp static AST analysis.

Usage:
    .venv/Scripts/python.exe tools/gen_import_graph.py          # regenerate
    .venv/Scripts/python.exe tools/gen_import_graph.py --check  # verify freshness (no write)
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

import grimp

PACKAGE = "agent_takkub"
OUT = Path(__file__).parent.parent / "docs" / "architecture" / "depgraph.json"

# Not part of the graph's semantic content — just provenance (which grimp
# version produced the file). A grimp point release must never fail the
# freshness check on its own, so this key is excluded from comparisons.
PROVENANCE_KEYS = {"generated_by"}


def build() -> dict:
    g = grimp.build_graph(PACKAGE)
    modules_sorted = sorted(g.modules)

    entries = []
    for mod in modules_sorted:
        imports = sorted(g.find_modules_directly_imported_by(mod))
        imported_by = sorted(g.find_modules_that_directly_import(mod))
        entries.append(
            {
                "module": mod,
                "imports": imports,
                "imported_by": imported_by,
                "fan_in": len(imported_by),
                "fan_out": len(imports),
            }
        )

    return {
        "generated_by": f"grimp {version('grimp')}",
        "module_count": len(modules_sorted),
        "modules": entries,
    }


def _diff_modules(committed_modules: list[dict], fresh_modules: list[dict]) -> list[str]:
    """Detailed module/edge-level diff for the "modules" key."""
    lines: list[str] = []

    committed_mods = {m["module"]: m for m in committed_modules}
    fresh_mods = {m["module"]: m for m in fresh_modules}

    for mod in sorted(fresh_mods.keys() - committed_mods.keys()):
        lines.append(f"+ module added: {mod}")
    for mod in sorted(committed_mods.keys() - fresh_mods.keys()):
        lines.append(f"- module removed: {mod}")

    for mod in sorted(committed_mods.keys() & fresh_mods.keys()):
        old, new = committed_mods[mod], fresh_mods[mod]
        for field in ("imports", "imported_by"):
            old_edges, new_edges = set(old[field]), set(new[field])
            for edge in sorted(new_edges - old_edges):
                lines.append(f"+ {mod} {field}: {edge}")
            for edge in sorted(old_edges - new_edges):
                lines.append(f"- {mod} {field}: {edge}")
        # fan_in/fan_out are derived from imported_by/imports but are stored
        # fields in their own right — a tampered or stale value here would
        # slip past the edge-set comparison above, so check them directly.
        for field in ("fan_in", "fan_out"):
            if old[field] != new[field]:
                lines.append(f"~ {mod} {field}: {old[field]} -> {new[field]}")

    return lines


def _diff(committed: dict, fresh: dict) -> list[str]:
    """Return human-readable lines describing semantic drift, ignoring PROVENANCE_KEYS.

    Every top-level key is compared, not just "modules" — an unknown/future
    key falls back to a generic added/removed/changed report so the check
    never goes silently blind to new fields in build()'s output.
    """
    lines: list[str] = []

    for key in sorted((committed.keys() | fresh.keys()) - PROVENANCE_KEYS):
        if key not in committed:
            lines.append(f"+ key added: {key} = {fresh[key]!r}")
        elif key not in fresh:
            lines.append(f"- key removed: {key} = {committed[key]!r}")
        elif key == "modules":
            lines.extend(_diff_modules(committed[key], fresh[key]))
        elif committed[key] != fresh[key]:
            lines.append(f"~ {key}: {committed[key]!r} -> {fresh[key]!r}")

    return lines


def check() -> bool:
    """Regenerate the graph in memory and compare it to the committed file.

    Returns True if fresh (no semantic drift). Provenance fields (generated_by)
    are ignored so a new grimp release alone never fails this check.
    """
    if not OUT.exists():
        print(f"depgraph check: {OUT} does not exist — run without --check to generate it")
        return False

    committed = json.loads(OUT.read_text(encoding="utf-8"))
    fresh = build()

    diff_lines = _diff(committed, fresh)
    if diff_lines:
        print(f"depgraph check: {OUT} is stale — drift detected:")
        for line in diff_lines:
            print(f"  {line}")
        print("Run: python tools/gen_import_graph.py")
        return False

    print(f"depgraph check: {OUT} is fresh (module_count={fresh['module_count']})")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed depgraph matches a fresh regeneration, without writing",
    )
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check() else 1)

    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"module_count={data['module_count']} -> {OUT}")


if __name__ == "__main__":
    main()
