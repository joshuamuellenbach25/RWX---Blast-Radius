"""Store the top-level import graph of the pinned sqlglot checkout in
data/imports.json, so the selector does not read 250 files at run time.

    python tools/build_imports.py SQLGLOT_CHECKOUT
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from blast.analysis import IMPORT_GRAPH_PATH, build_import_graph  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tools/build_imports.py SQLGLOT_CHECKOUT")
    source_root = Path(sys.argv[1])
    graph = build_import_graph(source_root)
    data = {"format": "blast-radius-imports/1", "graph": {rel: sorted(imports) for rel, imports in sorted(graph.items())}}
    IMPORT_GRAPH_PATH.write_text(json.dumps(data, separators=(",", ":")) + "\n", "utf-8")
    print(f"{len(graph)} files -> {IMPORT_GRAPH_PATH}")


if __name__ == "__main__":
    main()
