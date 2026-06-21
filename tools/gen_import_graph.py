"""
Generate docs/architecture/depgraph.json using grimp static AST analysis.

Usage:
    .venv/Scripts/python.exe tools/gen_import_graph.py
"""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import grimp

PACKAGE = "agent_takkub"
OUT = Path(__file__).parent.parent / "docs" / "architecture" / "depgraph.json"


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


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"module_count={data['module_count']} -> {OUT}")


if __name__ == "__main__":
    main()
