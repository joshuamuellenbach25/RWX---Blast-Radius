"""Collect a per-test coverage map of the pinned sqlglot checkout.

Runs inside the lab image (cwd /sqlglot, PYTHONPATH=/sqlglot). The parent
imports every sqlglot module and every test module once, recording the lines
those imports execute. Then every test runs alone in a fork of that parent,
so no cache or lazy state from an earlier test hides a dependency. Each fork
records:

- every (file, line) it executed under /sqlglot/sqlglot and /sqlglot/tests,
- every non-Python file under /sqlglot it opened (fixtures),
- whether the test passed, and its CPU seconds.

Output: one JSON file per test under <out_dir>/tests/<number>.json, plus
<out_dir>/import.json with the lines executed while importing.

Usage: python collect_map.py <tests.json> <out_dir> [--jobs 3] [--only N,N]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import resource
import sys
import time
import unittest
from pathlib import Path

ROOT = "/sqlglot/"
TRACKED_PREFIXES = ("/sqlglot/sqlglot/", "/sqlglot/tests/")
TOOL_ID = sys.monitoring.COVERAGE_ID

recorded_lines: set[tuple[str, int]] = set()
recorded_opens: set[str] = set()


def on_line(code, line_number):
    filename = code.co_filename
    if filename.startswith(TRACKED_PREFIXES):
        recorded_lines.add((filename, line_number))
    return sys.monitoring.DISABLE


def on_audit(event, arguments):
    if event != "open":
        return
    path = arguments[0]
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    if not isinstance(path, str):
        return
    path = os.path.abspath(path)
    if path.startswith(ROOT) and not path.endswith((".py", ".pyc")):
        recorded_opens.add(path)


def take_records() -> tuple[list, list]:
    lines = sorted(recorded_lines)
    opens = sorted(recorded_opens)
    recorded_lines.clear()
    recorded_opens.clear()
    return lines, opens


def group_lines(lines: list[tuple[str, int]]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for filename, line_number in lines:
        grouped.setdefault(filename[len(ROOT):], []).append(line_number)
    return grouped


def start_monitoring() -> None:
    sys.monitoring.use_tool_id(TOOL_ID, "blast-radius-map")
    sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.LINE, on_line)
    sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.LINE)
    sys.addaudithook(on_audit)


def import_everything(tests: list[dict]) -> dict:
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception as error:  # a module the suite never imports either
            print(f"import skipped {info.name}: {error!r}", file=sys.stderr)
    sqlglot_lines, sqlglot_opens = take_records()

    per_test_module: dict[str, dict] = {}
    for module_name in sorted({module_name_of(test) for test in tests}):
        sys.monitoring.restart_events()
        importlib.import_module(module_name)
        lines, opens = take_records()
        per_test_module[module_name] = {"lines": group_lines(lines), "opens": opens}
    return {
        "sqlglot": {"lines": group_lines(sqlglot_lines), "opens": sqlglot_opens},
        "test_modules": per_test_module,
    }


def module_name_of(test: dict) -> str:
    return test["file"][: -len(".py")].replace("/", ".")


def run_one_test(test: dict) -> dict:
    _, class_name, method_name = test["id"].split("::")
    module = sys.modules[module_name_of(test)]
    case = getattr(module, class_name)(method_name)
    suite = unittest.TestSuite([case])
    result = unittest.TestResult()
    sys.monitoring.restart_events()
    take_records()
    started = os.times()
    suite.run(result)
    finished = os.times()
    lines, opens = take_records()
    peak_megabytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    problems = [
        f"{kind}: {text[-400:]}"
        for kind, entries in (("failure", result.failures), ("error", result.errors))
        for _, text in entries
    ]
    return {
        "number": test["number"],
        "id": test["id"],
        "ok": not problems and result.testsRun == 1,
        "problems": problems,
        "cpu": round((finished.user - started.user) + (finished.system - started.system), 4),
        "peak_mb": peak_megabytes,
        "lines": group_lines(lines),
        "opens": opens,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tests_json", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--only", default="", help="comma separated test numbers")
    options = parser.parse_args()

    tests = json.loads(options.tests_json.read_text("utf-8"))["tests"]
    if options.only:
        wanted = {int(number) for number in options.only.split(",")}
        tests = [test for test in tests if test["number"] in wanted]
    test_dir = options.out_dir / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)

    start_monitoring()
    import_record = import_everything(tests)
    (options.out_dir / "import.json").write_text(json.dumps(import_record), "utf-8")

    def missing() -> list[dict]:
        # Slow tests last, so a stop leaves the most tests done.
        return sorted(
            (test for test in tests if not (test_dir / f"{test['number']}.json").exists()),
            key=lambda test: test["seconds"],
        )

    run_batch(missing(), options.jobs, test_dir)
    # A fork killed for memory while others ran gets one more try alone.
    retry = missing()
    if retry:
        print(f"retrying {len(retry)} alone", file=sys.stderr, flush=True)
        run_batch(retry, 1, test_dir)
    still_missing = missing()
    print(f"unmapped: {[test['number'] for test in still_missing]}", file=sys.stderr, flush=True)


def run_batch(pending: list[dict], jobs: int, test_dir: Path) -> None:
    pending = list(pending)
    print(f"{len(pending)} tests to map with {jobs} jobs", file=sys.stderr, flush=True)
    running: dict[int, int] = {}
    started_at = time.monotonic()
    done = 0
    while pending or running:
        while pending and len(running) < jobs:
            test = pending.pop(0)
            child = os.fork()
            if child == 0:
                code = 0
                try:
                    record = run_one_test(test)
                    target = test_dir / f"{test['number']}.json"
                    temporary = target.with_suffix(".tmp")
                    temporary.write_text(json.dumps(record), "utf-8")
                    os.replace(temporary, target)
                except BaseException as error:  # report and exit nonzero
                    print(f"child {test['number']} crashed: {error!r}", file=sys.stderr, flush=True)
                    code = 3
                finally:
                    os._exit(code)
            running[child] = test["number"]
        child, status = os.wait()
        if child not in running:
            # A helper process some import started, not one of the test forks.
            print(f"reaped unknown child {child}", file=sys.stderr, flush=True)
            continue
        number = running.pop(child)
        done += 1
        if os.waitstatus_to_exitcode(status) != 0:
            print(f"test {number} child exit {os.waitstatus_to_exitcode(status)}", file=sys.stderr, flush=True)
        if done % 100 == 0:
            print(f"{done} mapped, {time.monotonic() - started_at:.0f}s", file=sys.stderr, flush=True)
    print(f"finished {done} in {time.monotonic() - started_at:.0f}s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
