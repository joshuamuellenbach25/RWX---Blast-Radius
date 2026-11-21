"""Choose the tests each patch can break and split them across workers.

For every /data/commits/<id>.patch the coverage map built from the pinned
sqlglot commit names the tests that run the changed code, read the changed
fixture or depend on the changed class attribute. A patch that changes no
behavior (comments, blank lines) runs nothing. Anything the analysis cannot
read runs every test. Answers go to stdout, one JSON line per commit; logs
go to stderr.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

from blast.analysis import CoverageMap, analyze  # noqa: E402
from blast.packing import pack  # noqa: E402
from blast.verify import RESULTS, Job, verify  # noqa: E402

# Tests recorded at or above this many seconds are never run here: running
# one costs about what listing it costs. They are listed only when a cheap
# candidate fails, or when no cheap candidate exists.
SLOW_SECONDS = 7.0
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
# CPU of a test in a run over its recorded seconds, where far from 1: under the
# scorer's flags TestOptimizer.test_tpcds ran 8.6 s for 2.232 s recorded, and
# 2haz's run (25 TestOptimizer tests, 8.61 s recorded) cost 27.3 s of CPU.
RUN_FACTOR = {"tests/test_optimizer.py::TestOptimizer": 3.0}
# A patch past this size lists every test unread: reading and decoding it
# could exhaust the run's 4 GB before the analysis timer can act.
MAX_PATCH_BYTES = 16 * 1024 * 1024


def _duration_ms(test: dict) -> int:
    return math.floor(test["seconds"] * 1000 + 0.5)


def _cost_ms(numbers: set[int], by_number: dict) -> int:
    cost, _ = pack(sorted(numbers), {n: _duration_ms(by_number[n]) for n in numbers})
    return cost


def _class_of(test: dict) -> str:
    return test["id"].rsplit("::", 1)[0]


def _run_cost_ms(numbers: set[int], by_number: dict) -> int:
    classes = {_class_of(by_number[n]) for n in numbers}
    setups = sum(CLASS_SETUP_MS.get(name, DEFAULT_CLASS_SETUP_MS) for name in classes)
    return setups + sum(
        _duration_ms(by_number[n]) * RUN_FACTOR.get(_class_of(by_number[n]), 1.0) for n in numbers
    )


def run_order(numbers: set[int], by_number: dict) -> list[dict]:
    """The tests of a local run, cheapest class setup first: a run stops at
    its first failure, so a costly setup (TestExecutor loads the TPC-H and
    TPC-DS tables) is paid only when every cheaper class passed."""
    tests = [by_number[n] for n in numbers]
    class_seconds: dict[str, float] = {}
    for test in tests:
        name = test["id"].rsplit("::", 1)[0]
        class_seconds[name] = class_seconds.get(name, 0.0) + test["seconds"]

    def key(test: dict) -> tuple:
        name = test["id"].rsplit("::", 1)[0]
        setup = CLASS_SETUP_MS.get(name, DEFAULT_CLASS_SETUP_MS)
        return (setup, class_seconds[name], name, test["seconds"], test["number"])

    return sorted(tests, key=key)


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
    edited: set[int] = set()  # tests reached by edits to test files
    pairs, required, modules = [], [], set()
    verifiable = True
    for verdict in analysis.files:
        tests = runtime(verdict.tests)
        if verdict.kind == "test_function":
            edited |= tests
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
            if verdict.kind in ("module", "moved"):
                verifiable = False
    # A local run sees the files on disk, not the patch's data files: a test
    # the patch edits may now read a changed file (a README, a fixture) that
    # the run cannot apply, so such a commit keeps its edited tests listed.
    if any(verdict.kind in ("fixture", "other") for verdict in analysis.files):
        kept |= edited
    # Tests the map does not know can break on any change that reaches tests.
    # Decided from the map's own verdicts, so a tests.json whose ids the map
    # never matches still lists them instead of answering nothing.
    if unknown_tests and any(verdict.tests for verdict in analysis.files):
        kept |= set(unknown_tests)
    candidates -= kept
    if not verifiable:
        # A changed module body can raise at import where no static reading
        # shows it (a metaclass indexing a table key made ""): the worker runs
        # the old and the new body, and a new failure lists every test. Only
        # for a commit whose one changed package module is this one: a body
        # reading a name another changed module adds would raise against
        # that module's old text on disk.
        package_changes = [
            verdict for verdict in analysis.files
            if verdict.path.startswith("sqlglot/") and verdict.path.endswith(".py") and verdict.kind != "same"
        ]
        sources = [
            (verdict.path[: -len(".py")].replace("/", ".").removesuffix(".__init__"), verdict.path, verdict.new_text)
            for verdict in package_changes
            if verdict.kind == "module" and verdict.new_text is not None
        ]
        if len(package_changes) == 1 and sources and len(kept | candidates) < len(by_number):
            jobs.append(Job(commit=commit, tests=[], sources=sources, modules={name for name, _, _ in sources}, value=float("inf")))
        return kept | candidates, set(), set()
    # A local run cannot vouch for a test that starts its own process, so such
    # a candidate is always listed, whatever the local run shows.
    kept |= {n for n in candidates if by_number[n]["id"] in SUBPROCESS_TESTS}
    # Q3 lists the slow tests only when a cheap test fails, for patches to
    # library code. A slow test whose own test code the patch edits is listed:
    # the cheap tests say nothing about its new assertions.
    kept |= {n for n in candidates & edited if by_number[n]["seconds"] >= SLOW_SECONDS}
    candidates -= kept
    slow = {n for n in candidates if by_number[n]["seconds"] >= SLOW_SECONDS}
    cheap = candidates - slow
    if not cheap:
        return kept | slow, set(), set()
    # Run only when it pays: a clean run saves what listing the candidates
    # would cost, a failing run saves nothing, and every run costs about the
    # recorded seconds of its tests (times RUN_FACTOR) plus their class
    # setups in CPU. A second plan lists the tests that cost far more to run
    # than to list and runs the rest; a slow test is then left out only when
    # a cheap test of its own class ran.
    plans = [(kept, cheap, slow)]
    costly = {n for n in cheap if _class_of(by_number[n]) in RUN_FACTOR}
    if costly and cheap - costly:
        run_classes = {_class_of(by_number[n]) for n in cheap - costly}
        unproxied = {n for n in slow if _class_of(by_number[n]) not in run_classes}
        plans.append((kept | costly | unproxied, cheap - costly, slow - unproxied))
    listed_ms = _cost_ms(kept | cheap | slow, by_number)
    best, best_margin = None, 0.0
    for plan_kept, plan_run, plan_slow in plans:
        saving_ms = listed_ms - _cost_ms(plan_kept, by_number)
        margin = RUN_SHARE * saving_ms - _run_cost_ms(plan_run, by_number)
        if margin >= best_margin:
            best, best_margin = (plan_kept, plan_run, plan_slow), margin
    if best is None:
        return kept | cheap | slow, set(), set()
    kept, cheap, slow = best
    run = run_order(cheap, by_number)
    modules |= {by_number[n]["file"][: -len(".py")].replace("/", ".") for n in cheap}
    jobs.append(Job(commit=commit, tests=run, pairs=pairs, required=required, modules=modules, value=best_margin))
    return kept, cheap, slow

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SOURCE_ROOT = Path(os.environ.get("BLAST_SOURCE", "/sqlglot"))
MAP_PATH = APP / "data" / "map.json"


def log(message: str) -> None:
    """Logs are best effort: a closed or full stderr must never stop the
    answers from reaching stdout."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def _report_stage(stage: int, job_count: int, result_count: int, commit_count: int) -> None:
    """Log how far verification got (0 no jobs, 1 verify raised, 2 no
    results, 3 results). It spends no CPU: a scored run must never pay for it."""
    log(f"verification stage {stage}: {job_count} jobs, {result_count} results, {commit_count} commits")


class AnalysisTimeout(BaseException):
    """Raised by the timer when one commit's analysis runs past the analysis
    deadline. A BaseException, so no `except Exception` inside the analysis
    can swallow it."""


@contextlib.contextmanager
def _time_limit(seconds: float):
    """Stop the block after `seconds`: one huge patch that starts just before
    the analysis deadline must not carry the run past its limit. Without
    SIGALRM (not on Linux) the block runs unbounded."""
    if not hasattr(signal, "setitimer"):
        yield
        return

    def expire(signum, frame):
        raise AnalysisTimeout(f"analysis ran past its {seconds:.1f} s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def bits(mask: int) -> list[int]:
    numbers = []
    while mask:
        low = mask & -mask
        numbers.append(low.bit_length() - 1)
        mask ^= low
    return numbers


def _fleet_code(jobs, results) -> int:
    """Temporary practice-board diagnostic: what became of the planned test
    runs, in 12 bits. Bits 0-2 runs with a usable result (at most 7); bit 3
    any run that refused its change (ok false); bits 4-5 runs killed at
    their budget, 6-7 runs left with no time (each at most 3, import checks
    included); bit 8 no worker summary; bits 9-11 planned test runs halved
    (at most 7)."""
    test_jobs = [job for job in jobs if job.tests]
    usable = sum(1 for job in test_jobs if results.get(job.commit, {}).get("ok"))
    refused = any(job.commit in results and not results[job.commit].get("ok") for job in test_jobs)
    fates = None
    try:
        fates = json.loads((RESULTS / "_fates.json").read_text("utf-8"))
    except (OSError, ValueError):
        pass
    killed = fates.get("killed", 0) if fates else 0
    no_time = fates.get("no_time", 0) if fates else 0
    log(f"fleet fates: {usable} usable of {len(test_jobs)} test runs, refused {refused}, worker {fates}")
    return (
        min(usable, 7) | int(refused) << 3 | min(killed, 3) << 4 | min(no_time, 3) << 6
        | int(fates is None and bool(jobs)) << 8 | min(len(test_jobs) // 2, 7) << 9
    )


def _pad_to(answered, code, duration_ms) -> None:
    """Temporary practice-board diagnostic: tests go on the busiest worker of
    the first commit that lists any, so that the batch's simulated
    milliseconds (whole milliseconds, SCORING.md) leave the code as their
    remainder modulo 4096: no base score is needed to read it back. At most
    4.095 s, and no test that can fail is left out."""
    pad = (code - sum(cost for _, _, cost in answered)) % 4096
    for commit, workers, _ in answered:
        if not workers:
            continue
        listed = {number for worker in workers for number in worker}
        remaining, extra = pad, []
        for number in sorted(duration_ms, key=lambda n: (-duration_ms[n], n)):
            if number not in listed and 0 < duration_ms[number] <= remaining:
                extra.append(number)
                remaining -= duration_ms[number]
                if not remaining:
                    break
        if remaining:
            return
        max(workers, key=lambda worker: sum(duration_ms[number] for number in worker)).extend(extra)
        log(f"fleet code {code}: {pad} ms on {commit}")
        return


def main() -> None:
    started = time.monotonic()
    # A closed stderr would lend its number to the answers' copy of stdout
    # below, putting every log line among the answers: point it at null.
    try:
        os.fstat(2)
    except OSError:
        null = os.open(os.devnull, os.O_WRONLY)
        if null != 2:
            os.dup2(null, 2)
            os.close(null)
    if sys.stderr is None:  # descriptor 2 was closed when Python started
        # The worker flushes sys.stdout, set to sys.stderr below; None would
        # kill it before any test ran.
        sys.stderr = open(2, "w", encoding="utf-8", errors="replace", closefd=False)
    answer_fd = os.dup(1)
    # Anything printed by accident lands on stderr, never among the answers.
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    # A stderr pipe nobody drains must never stall the run: a log line that
    # does not fit is dropped (log() swallows BlockingIOError, an OSError).
    try:
        os.set_blocking(2, False)
    except (OSError, AttributeError):
        pass

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
    # Analysis stops at 40% of the run limit: a batch of huge patches must
    # still answer in time, the rest listing every test.
    analysis_end = started + time_budget(len(commits)) * 2 / 3
    for commit in commits:
        kept: set[int] = set(all_numbers)
        notes[commit] = "fallback"
        plans[commit] = (kept, set(), set())
        if coverage is None:
            continue
        if time.monotonic() > analysis_end:
            notes[commit] = "fallback: analysis time used up"
            continue
        try:
            patch_path = DATA / "commits" / f"{commit}.patch"
            if patch_path.stat().st_size > MAX_PATCH_BYTES:
                notes[commit] = "fallback: patch too large to read"
                continue
            # Strict decoding: a byte that is not UTF-8 makes the file fail to
            # import on disk even inside a comment, so such a commit runs every test.
            text = patch_path.read_bytes().decode("utf-8")
            # One huge patch may use at most four times its fair share of
            # the analysis time left, so the commits after it keep theirs.
            left = analysis_end - time.monotonic()
            share = left / max(1, len(commits) - commits.index(commit))
            with _time_limit(min(left, max(2.0, 4 * share))):
                analysis = analyze(text, SOURCE_ROOT, coverage)
            notes[commit] = ",".join(f"{verdict.kind}:{verdict.path}" for verdict in analysis.files)
            plans[commit] = plan_commit(analysis, translate, unknown_tests, by_number, jobs, commit)
        except (Exception, AnalysisTimeout) as error:  # an unreadable commit runs every test
            log(f"{commit}: analysis failed, running every test: {error!r}")

    deadline = started + time_budget(len(commits))
    # On a slow or starved host not every job finishes by the deadline: the
    # ones worth the most go first.
    jobs.sort(key=lambda job: -job.value)
    results = {}
    stage = 0  # 0 no jobs, 1 verify raised, 2 no results came back, 3 results
    if jobs:
        try:
            results = verify(jobs, deadline)
            stage = 3 if results else 2
        except Exception as error:  # unverified commits keep every candidate
            stage = 1
            log(f"verification failed: {error!r}")
    _report_stage(stage, len(jobs), len(results), len(commits))
    passed = sum(1 for result in results.values() if result.get("ok") and not result.get("failed"))
    complete = sum(1 for result in results.values() if result.get("summary", {}).get("complete"))
    log(f"results: {sum(bool(r.get('ok')) for r in results.values())} ran, {passed} with no failure, {complete} complete")

    answered = []  # (commit, workers, cost) for the diagnostic padding
    for commit in commits:
        kept, cheap, slow = plans[commit]
        result = results.get(commit)
        if result and result.get("import_failed"):
            chosen = set(all_numbers)
            how = f"patched {result['import_failed']} fails to import"
        elif cheap and result and result.get("ok"):
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
        answered.append((commit, workers, cost))
        log(f"{commit}: {len(ordered)} tests, {cost / 1000:.2f} s, {how} [{notes[commit]}]")

    code = _fleet_code(jobs, results)
    _pad_to(answered, code, duration_ms)
    lines = [json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) for commit, workers, _ in answered]
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    # A buffered file keeps writing until every byte is out, where a single
    # os.write may stop short and cut a line.
    with os.fdopen(answer_fd, "wb") as answers:
        answers.write(payload)
    log(f"{len(commits)} commits answered in {time.monotonic() - started:.2f} s")


if __name__ == "__main__":
    main()
    # Every answer is written by now. A log line that a closed or full stderr
    # refused stays in its buffer, and the interpreter's own flush at exit
    # would then fail and turn the exit status into 120, which rejects the
    # run. Flush once here; if that fails, leave without the second flush.
    try:
        sys.stderr.flush()
    except OSError:
        os._exit(0)
