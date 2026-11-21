"""Pack the per-test coverage records into one compact map file.

Usage: python build_map.py <collect_dir> <tests.json> <source_root> <out.json>

The map is JSON text. Each distinct set of tests is stored once in "sets" as
flat inclusive ranges of test numbers; the tables below point at a set. It holds:
- lines[file][line]: tests that executed that line,
- opens[file]: tests that opened that non-Python file,
- import_lines[file]: lines executed while importing sqlglot and the tests,
- classes["module:qualname"]: tests that created an object of that class,
- names[name]: tests that executed a line naming `name` (attribute, global
  or module name) in sqlglot or the tests,
- import_names: names that some line executed at import time refers to,
- cpu[number], peak_mb[number], baseline_failures, unmapped: from the run.
"""

from __future__ import annotations

import dis
import json
import sys
from pathlib import Path
from types import CodeType

READ_OPS = {
    "LOAD_ATTR", "LOAD_METHOD", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_SUPER_ATTR",
    "STORE_ATTR", "DELETE_ATTR", "LOAD_FROM_DICT_OR_GLOBALS",
}
IMPORT_OPS = {"IMPORT_NAME", "IMPORT_FROM"}


def name_lines(source_root: Path, ops: set[str]) -> dict[str, dict[str, set[int]]]:
    """For every Python file under sqlglot/ and tests/: name -> lines where
    one of `ops` names it."""
    per_file: dict[str, dict[str, set[int]]] = {}
    for top in ("sqlglot", "tests"):
        for path in sorted((source_root / top).rglob("*.py")):
            relative = path.relative_to(source_root).as_posix()
            try:
                code = compile(path.read_text("utf-8"), "/sqlglot/" + relative, "exec", dont_inherit=True)
            except (SyntaxError, UnicodeDecodeError, ValueError) as error:
                print(f"skipped {relative}: {error}", file=sys.stderr)
                continue
            names: dict[str, set[int]] = {}
            stack = [code]
            while stack:
                node = stack.pop()
                for instruction in dis.get_instructions(node):
                    if instruction.opname in ops and isinstance(instruction.argval, str):
                        line = instruction.positions.lineno if instruction.positions else None
                        if line is not None:
                            for part in instruction.argval.split("."):
                                names.setdefault(part, set()).add(line)
                for const in node.co_consts:
                    if isinstance(const, CodeType):
                        stack.append(const)
            per_file[relative] = names
    return per_file


def import_time_code(tests: list[dict]) -> set[tuple[str, str, int]]:
    """(file, qualname, first line) of every sqlglot or test code object that
    starts running while sqlglot and the test modules import. Must run
    before anything imports sqlglot."""
    import importlib
    import pkgutil

    started: set[tuple[str, str, int]] = set()
    tool = sys.monitoring.PROFILER_ID

    def on_start(code, offset):
        if code.co_filename.startswith("/sqlglot/"):
            started.add((code.co_filename[len("/sqlglot/"):], code.co_qualname, code.co_firstlineno))
        return sys.monitoring.DISABLE

    sys.monitoring.use_tool_id(tool, "blast-radius-import")
    sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, on_start)
    sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)
    try:
        import sqlglot

        for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
            if info.name != "sqlglot.__main__":
                try:
                    importlib.import_module(info.name)
                except Exception as error:  # the suite never imports these either
                    print(f"import skipped {info.name}: {error!r}", file=sys.stderr)
        for module_name in sorted({test["file"][:-3].replace("/", ".") for test in tests}):
            importlib.import_module(module_name)
    finally:
        sys.monitoring.set_events(tool, 0)
        sys.monitoring.free_tool_id(tool)
    return started


def class_bases() -> dict[str, list[str]]:
    """Every sqlglot class -> its direct sqlglot base classes, as
    "module:qualname" keys. Needs sqlglot importable."""
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception as error:  # the suite never imports these either
                print(f"hierarchy skipped {info.name}: {error!r}", file=sys.stderr)

    def key(cls: type) -> str:
        return f"{cls.__module__}:{cls.__qualname__}"

    roots: set[type] = set()
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("sqlglot") or module is None:
            continue
        stack = [value for value in vars(module).values() if isinstance(value, type)]
        while stack:
            cls = stack.pop()
            if cls in roots or not cls.__module__.startswith("sqlglot"):
                continue
            roots.add(cls)
            stack.extend(value for value in vars(cls).values() if isinstance(value, type))
            stack.extend(type.__subclasses__(cls))
    bases: dict[str, list[str]] = {}
    for cls in roots:
        parents = [key(base) for base in cls.__bases__ if base.__module__.startswith("sqlglot")]
        if parents:
            bases[key(cls)] = parents
    return bases


def to_ranges(mask: int) -> list[int]:
    """A test set as flat inclusive ranges: [first, last, first, last, ...]."""
    flat: list[int] = []
    number = 0
    while mask:
        if mask & 1:
            start = number
            while mask & 1:
                mask >>= 1
                number += 1
            flat.extend((start, number - 1))
        else:
            skip = (mask & -mask).bit_length() - 1
            mask >>= skip
            number += skip
    return flat


def main() -> None:
    collect_dir, tests_json, source_root, out_path = (Path(argument) for argument in sys.argv[1:5])
    tests = json.loads(tests_json.read_text("utf-8"))["tests"]
    import_code = import_time_code(tests)
    lines: dict[str, dict[int, int]] = {}
    opens: dict[str, int] = {}
    classes: dict[str, int] = {}
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
        for class_key in record.get("classes", []):
            classes[class_key] = classes.get(class_key, 0) | bit
        cpu[number] = record["cpu"]
        peak[number] = record.get("peak_mb", 0)
        if not record["ok"]:
            failures.append(number)

    imports = json.loads((collect_dir / "import.json").read_text("utf-8"))
    import_lines: dict[str, set[int]] = {
        name: set(values) for name, values in imports["sqlglot"]["lines"].items()
    }
    for record in imports["test_modules"].values():
        for name, values in record["lines"].items():
            import_lines.setdefault(name, set()).update(values)

    # names: tests that ran a line reading or importing the name.
    # import_names: names some line reads (not merely imports) at import time,
    # so a change to them can reach other objects built during import.
    names: dict[str, int] = {}
    import_names: set[str] = set()
    for ops, record_import in ((READ_OPS, True), (IMPORT_OPS, False)):
        for filename, per_name in name_lines(source_root, ops).items():
            file_lines = lines.get(filename, {})
            file_import_lines = import_lines.get(filename, set())
            for name, name_line_set in per_name.items():
                mask = 0
                for line in name_line_set:
                    mask |= file_lines.get(line, 0)
                if mask:
                    names[name] = names.get(name, 0) | mask
                if record_import and name_line_set & file_import_lines:
                    import_names.add(name)

    # Every distinct test set is stored once; the tables point at its index.
    set_index: dict[int, int] = {}
    sets: list[list[int]] = []

    def index_of(mask: int) -> int:
        if mask not in set_index:
            set_index[mask] = len(sets)
            sets.append(to_ranges(mask))
        return set_index[mask]

    packed = {
        "format": "blast-radius-map/1",
        "test_count": len(tests),
        "test_ids": [test["id"] for test in sorted(tests, key=lambda test: test["number"])],
        "sets": sets,
        # file -> [line, set index, line, set index, ...]
        "lines": {
            filename: [value for line in sorted(per_file) for value in (line, index_of(per_file[line]))]
            for filename, per_file in sorted(lines.items())
        },
        "opens": {path: index_of(mask) for path, mask in sorted(opens.items())},
        "classes": {name: index_of(mask) for name, mask in sorted(classes.items())},
        "names": {name: index_of(mask) for name, mask in sorted(names.items())},
        "import_lines": {name: sorted(values) for name, values in sorted(import_lines.items())},
        "import_names": sorted(import_names),
        "import_code": sorted([list(entry) for entry in import_code]),
        "bases": dict(sorted(class_bases().items())),
        "cpu": [cpu.get(number, 0.0) for number in range(len(tests))],
        "peak_mb": [peak.get(number, 0) for number in range(len(tests))],
        "baseline_failures": failures,
        "unmapped": missing,
    }
    text = json.dumps(packed, separators=(",", ":"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, "utf-8", newline="\n")
    line_count = sum(len(per_file) for per_file in lines.values())
    print(
        f"{len(tests) - len(missing)} tests mapped, {len(missing)} missing {missing[:10]}, "
        f"{len(failures)} failed in collection {failures[:10]}, {len(lines)} files, {line_count} lines, "
        f"{len(classes)} classes, {len(names)} names, {len(sets)} distinct test sets, {len(text)} bytes",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
