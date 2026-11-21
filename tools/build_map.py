"""Pack the per-test coverage records into one compact map file.

Usage: python build_map.py <collect_dir> <tests.json> <out.pkl.gz>

The map holds, with tests as bitmasks over test numbers:
- lines[file][line]: tests that executed that line,
- opens[file]: tests that opened that non-Python file,
- import_lines[file]: lines executed while importing sqlglot,
- module_import_lines[test module][file]: lines executed while importing it,
- cpu[number], peak_mb[number], baseline_failures: from the collection run.
"""

from __future__ import annotations

import gzip
import json
import pickle
import sys
from pathlib import Path


def main() -> None:
    collect_dir, tests_json, out_path = (Path(argument) for argument in sys.argv[1:4])
    tests = json.loads(tests_json.read_text("utf-8"))["tests"]
    interned: dict[int, int] = {}
    lines: dict[str, dict[int, int]] = {}
    opens: dict[str, int] = {}
    cpu: dict[int, float] = {}
    peak: dict[int, int] = {}
    failures: list[int] = []
    missing: list[int] = []

    for test in tests:
        number = test["number"]
        record_path = collect_dir / "tests" / f"{number}.json"
        if not record_path.exists():
            missing.append(number)
            continue
        record = json.loads(record_path.read_text("utf-8"))
        bit = 1 << number
        for filename, file_lines in record["lines"].items():
            per_file = lines.setdefault(filename, {})
            for line in file_lines:
                per_file[line] = per_file.get(line, 0) | bit
        for path in record["opens"]:
            relative = path[len("/sqlglot/"):]
            opens[relative] = opens.get(relative, 0) | bit
        cpu[number] = record["cpu"]
        peak[number] = record.get("peak_mb", 0)
        if not record["ok"]:
            failures.append(number)

    for per_file in lines.values():
        for line, mask in per_file.items():
            per_file[line] = interned.setdefault(mask, mask)

    imports = json.loads((collect_dir / "import.json").read_text("utf-8"))
    import_lines = {name: set(values) for name, values in imports["sqlglot"]["lines"].items()}
    module_import_lines = {
        module: {name: set(values) for name, values in record["lines"].items()}
        for module, record in imports["test_modules"].items()
    }
    packed = {
        "test_count": len(tests),
        "lines": lines,
        "opens": opens,
        "import_lines": import_lines,
        "module_import_lines": module_import_lines,
        "cpu": cpu,
        "peak_mb": peak,
        "baseline_failures": failures,
        "unmapped": missing,
    }
    data = gzip.compress(pickle.dumps(packed, protocol=pickle.HIGHEST_PROTOCOL), 9)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    line_count = sum(len(per_file) for per_file in lines.values())
    print(
        f"{len(tests) - len(missing)} tests mapped, {len(missing)} missing, "
        f"{len(failures)} failed in collection, {len(lines)} files, {line_count} lines, "
        f"{len(interned)} distinct test sets, {len(data)} bytes",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
