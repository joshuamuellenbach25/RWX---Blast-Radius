"""Choose the tests each patch can break and split them across workers.

For every /data/commits/<id>.patch the coverage map built from the pinned
sqlglot commit names the tests that run the changed code, read the changed
fixture or depend on the changed class attribute. A patch that changes no
behavior (comments, blank lines) runs nothing. Anything the analysis cannot
read runs every test. Answers go to stdout, one JSON line per commit; logs
go to stderr.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

from blast.analysis import CoverageMap, analyze  # noqa: E402
from blast.packing import pack  # noqa: E402
from blast.verify import Job, verify  # noqa: E402

# Tests recorded at or above this many seconds are never run here: running
# one costs about what listing it costs. They are listed only when a cheap
# candidate fails, or when no cheap candidate exists.
SLOW_SECONDS = 2.5
# Tests that start their own Python process see the files on disk, not the
# patched code, so a run here proves nothing about them.
SUBPROCESS_TESTS = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
VERIFIED_KINDS = {"function", "test_function"}
# A local run pays when its CPU stays under this share of what a clean run
# saves: about half the sample's verified commits break nothing.
RUN_SHARE = 0.5
# CPU of one class setup, by test class: TestExecutor loads the TPC-H and
# TPC-DS tables, TestOptimizer opens a DuckDB database.
CLASS_SETUP_MS = {
    "tests/test_executor.py::TestExecutor": 2000,
    "tests/test_optimizer.py::TestOptimizer": 500,
}
DEFAULT_CLASS_SETUP_MS = 50


def _duration_ms(test: dict) -> int:
    return math.floor(test["seconds"] * 1000 + 0.5)


def _cost_ms(numbers: set[int], by_number: dict) -> int:
    cost, _ = pack(sorted(numbers), {n: _duration_ms(by_number[n]) for n in numbers})
    return cost


def _run_cost_ms(numbers: set[int], by_number: dict) -> int:
    classes = {by_number[n]["id"].rsplit("::", 1)[0] for n in numbers}
    setups = sum(CLASS_SETUP_MS.get(name, DEFAULT_CLASS_SETUP_MS) for name in classes)
    return setups + sum(_duration_ms(by_number[n]) for n in numbers)


def time_budget(commit_count: int) -> float:
    """Seconds of wall time the whole batch may use, 60% of the run limit:
    3 minutes for practice-sized batches, 4.5 minutes for the final's."""
    limit = 180.0 if commit_count <= 45 else 270.0
    return 0.6 * limit


def plan_commit(analysis, translate, unknown_tests, by_number, jobs, commit):
    """(tests kept without running, cheap tests to run, slow tests) in the
    runtime numbering; adds a Job when some tests can be run here."""
    def runtime(mask: int) -> set[int]:
        return {translate[n] for n in bits(mask) if translate[n] is not None}

    kept: set[int] = set()
    candidates: set[int] = set()
    pairs, required, modules = [], [], set()
    verifiable = True
    for verdict in analysis.files:
        tests = runtime(verdict.tests)
        if verdict.kind in VERIFIED_KINDS:
            candidates |= tests
            for change in verdict.swaps:
                pairs.append((change.old, change.new))
                if "<locals>" not in change.old.co_qualname:
                    required.append(change.old)
            module = verdict.path[: -len(".py")].replace("/", ".")
            modules.add(module[: -len(".__init__")] if module.endswith(".__init__") else module)
        else:
            kept |= tests
            if verdict.kind == "module":
                verifiable = False
    if (kept or candidates) and unknown_tests:
        kept |= set(unknown_tests)
    candidates -= kept
    if not verifiable:
        return kept | candidates, set(), set()
    slow = {n for n in candidates if by_number[n]["seconds"] >= SLOW_SECONDS or by_number[n]["id"] in SUBPROCESS_TESTS}
    cheap = candidates - slow
    if not cheap:
        return kept | slow, set(), set()
    # Run only when it pays: a clean run saves what listing the candidates
    # would cost, a failing run saves nothing, and every run costs about the
    # recorded seconds of its tests plus their class setups in CPU.
    saving_ms = _cost_ms(kept | cheap | slow, by_number) - _cost_ms(kept, by_number)
    if _run_cost_ms(cheap, by_number) > RUN_SHARE * saving_ms:
        return kept | cheap | slow, set(), set()
    run = [by_number[n] for n in sorted(cheap)]
    modules |= {by_number[n]["file"][: -len(".py")].replace("/", ".") for n in cheap}
    jobs.append(Job(commit=commit, tests=run, pairs=pairs, required=required, modules=modules))
    return kept, cheap, slow

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SOURCE_ROOT = Path(os.environ.get("BLAST_SOURCE", "/sqlglot"))
MAP_PATH = APP / "data" / "map.json"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def bits(mask: int) -> list[int]:
    numbers = []
    while mask:
        low = mask & -mask
        numbers.append(low.bit_length() - 1)
        mask ^= low
    return numbers


def main() -> None:
    started = time.monotonic()
    answer_fd = os.dup(1)
    # Anything printed by accident lands on stderr, never among the answers.
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    all_numbers = sorted(test["number"] for test in tests)
    duration_ms = {test["number"]: math.floor(test["seconds"] * 1000 + 0.5) for test in tests}
    commits = sorted(path.stem for path in (DATA / "commits").glob("*.patch"))

    coverage = None
    try:
        coverage = CoverageMap(MAP_PATH)
    except Exception as error:  # without the map every commit runs every test
        log(f"map unreadable, running every test: {error!r}")
    translate: list[int | None] = []
    unknown_tests: list[int] = []
    if coverage is not None:
        number_by_id = {test["id"]: test["number"] for test in tests}
        translate = [number_by_id.get(test_id) for test_id in coverage.test_ids]
        known = set(coverage.test_ids)
        unknown_tests = [test["number"] for test in tests if test["id"] not in known]
        if unknown_tests:
            log(f"{len(unknown_tests)} tests are not in the map; they run with every changed commit")

    by_number = {test["number"]: test for test in tests}
    # commit -> (tests kept without running, cheap tests to run, slow tests)
    plans: dict[str, tuple[set[int], set[int], set[int]]] = {}
    jobs: list[Job] = []
    notes: dict[str, str] = {}
    for commit in commits:
        kept: set[int] = set(all_numbers)
        notes[commit] = "fallback"
        plans[commit] = (kept, set(), set())
        if coverage is None:
            continue
        try:
            text = (DATA / "commits" / f"{commit}.patch").read_text("utf-8", errors="replace")
            analysis = analyze(text, SOURCE_ROOT, coverage)
            notes[commit] = ",".join(f"{verdict.kind}:{verdict.path}" for verdict in analysis.files)
            plans[commit] = plan_commit(analysis, translate, unknown_tests, by_number, jobs, commit)
        except Exception as error:  # an unreadable commit runs every test
            log(f"{commit}: analysis failed, running every test: {error!r}")

    deadline = started + time_budget(len(commits))
    results = {}
    if jobs:
        try:
            results = verify(jobs, deadline)
        except Exception as error:  # unverified commits keep every candidate
            log(f"verification failed: {error!r}")

    lines = []
    for commit in commits:
        kept, cheap, slow = plans[commit]
        result = results.get(commit)
        if cheap and result and result.get("ok"):
            failed = set(result["failed"]) & cheap
            complete = bool(result.get("summary", {}).get("complete"))
            if failed:
                # A run that stopped at its first failure left tests unrun,
                # so every candidate is listed.
                chosen = kept | (failed if complete else cheap) | slow
            elif complete:
                chosen = kept
            else:
                chosen = kept | cheap | slow
            how = f"ran {result.get('summary', {}).get('run')} of {len(cheap)}, {len(failed)} failed"
        else:
            chosen = kept | cheap | slow
            how = "not run" if not cheap else f"no result {result.get('reason') if result else ''}"
        ordered = sorted(chosen)
        cost, workers = pack(ordered, duration_ms)
        lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
        log(f"{commit}: {len(ordered)} tests, {cost / 1000:.2f} s, {how} [{notes[commit]}]")

    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    os.write(answer_fd, payload)
    log(f"{len(commits)} commits answered in {time.monotonic() - started:.2f} s")


if __name__ == "__main__":
    main()
