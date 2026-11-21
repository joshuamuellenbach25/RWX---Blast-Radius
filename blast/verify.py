"""Run each commit's cheap candidate tests against the patched code.

The selector process never imports sqlglot. It forks one warm worker that
imports sqlglot and the needed test modules once; that worker forks one
child per commit, points the changed functions at their new code, runs the
commit's cheap candidate tests and writes the failing tests to /tmp. A
commit whose child crashes, times out or cannot apply its change gets no
result, and the selector then keeps every candidate for it.
"""

from __future__ import annotations

import gc
import importlib
import json
import os
import signal
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

RESULTS = Path("/tmp/blast-results")
# The run has 2 cores: one child per core.
PARALLEL_CHILDREN = 2


@dataclass
class Job:
    commit: str
    tests: list[dict]  # tests.json entries to run
    pairs: list = field(default_factory=list)  # (old code, new code)
    required: list = field(default_factory=list)  # old codes that must have live functions
    modules: set[str] = field(default_factory=set)  # modules to import before forking
    value: float = 0.0  # expected saving net of CPU, in ms: the selector runs the largest first
    # (module name, path under the source root, new text): each body runs as
    # an import would before any test, see _import_failure.
    sources: list = field(default_factory=list)


def _limit(test: dict) -> float:
    return 3.0 + 6.0 * test["seconds"]


def _body_runs(name: str, filename: str, text: str) -> bool:
    """Runs a module body in a fresh module object in a forked child, as an
    import of the module would; True when it raises nothing."""
    child = os.fork()
    if child == 0:
        code = 3
        try:
            # A body that never finishes must not outlive its job: the
            # alarm's default action ends this child after 10 s.
            signal.alarm(10)
            module = types.ModuleType(name)
            module.__file__ = filename
            module.__package__ = name if filename.endswith("__init__.py") else name.rpartition(".")[0]
            sys.modules[name] = module
            exec(compile(text, filename, "exec"), module.__dict__)
            code = 0
        except BaseException:  # SystemExit too: the import would fail
            pass
        finally:
            os._exit(code)
    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status) == 0


def _import_failure(sources: list, root: str = "/sqlglot") -> str:
    """The changed module whose new body raises where its old body ran clean,
    each run as an import would (a metaclass reading a table built with an
    empty key raises IndexError at import, which no static reading shows);
    "" when there is none. A module whose old body also raises when run
    again is no evidence either way."""
    for name, path, new_text in sources:
        filename = os.path.join(root, path)
        try:
            with open(filename, encoding="utf-8") as handle:
                old_text = handle.read()
        except OSError:
            continue
        if _body_runs(name, filename, old_text) and not _body_runs(name, filename, new_text):
            return name
    return ""


def _run_job(job: Job, index) -> dict:
    from blast import runner

    failure = _import_failure(job.sources)
    if failure:
        return {"commit": job.commit, "ok": True, "import_failed": failure, "failed": [], "summary": {}}
    if not job.tests:
        return {"commit": job.commit, "ok": True, "failed": [], "summary": {"complete": True, "run": 0}}
    counts = runner.swap_functions(job.pairs, index)
    swapped = sum(counts)
    # A function defined at import time must exist and must now run the new
    # code; otherwise the tests would run the old code and prove nothing.
    required = {id(old) for old in job.required}
    missing = [
        old.co_qualname
        for (old, _), count in zip(job.pairs, counts, strict=True)
        if id(old) in required and count == 0
    ]
    if missing:
        return {"commit": job.commit, "ok": False, "reason": f"no live function for {missing[:3]}"}
    limits = {test["number"]: _limit(test) for test in job.tests}
    # The first failure already decides that the commit's candidates get
    # listed, so the rest of the run would only cost CPU seconds.
    failed, summary = runner.run_tests(job.tests, limits, stop_at_first_failure=True)
    return {"commit": job.commit, "ok": True, "failed": sorted(failed), "swapped": swapped, "summary": summary}


def _warm_worker(jobs: list[Job], deadline: float) -> None:
    from blast import runner

    # The scorer may hand us a stderr pipe that nobody reads, or a closed one:
    # a test's output would then block or kill the worker and every child
    # before any result is written. Their output goes nowhere unless asked.
    # This comes before any flush: a log line a broken pipe refused is still
    # buffered, and flushing it into that pipe would end the worker.
    if not os.environ.get("BLAST_WORKER_STDERR"):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
    runner.quiet_stdout()
    os.chdir("/sqlglot")
    if "/sqlglot" not in sys.path:
        sys.path.insert(0, "/sqlglot")
    started = time.monotonic()
    import sqlglot  # noqa: F401

    for module in sorted({name for job in jobs for name in job.modules}):
        try:
            importlib.import_module(module)
        except BaseException as error:  # SystemExit too: the child reports the module's tests as failed
            print(f"warm import {module} failed: {error!r}", file=sys.stderr, flush=True)
    index = runner.function_index()
    # Children inherit this heap. Frozen objects are skipped by the children's
    # garbage collections, so a child neither walks nor copies those pages.
    gc.freeze()
    print(f"warm worker ready in {time.monotonic() - started:.1f} s, {len(index)} functions", file=sys.stderr, flush=True)
    pending = list(jobs)
    running: dict[int, tuple[Job, float, float]] = {}  # pid -> (job, start, budget)
    while pending or running:
        while pending and len(running) < PARALLEL_CHILDREN:
            job = pending.pop(0)
            remaining = deadline - time.monotonic()
            if remaining < 5:
                print(f"{job.commit}: no time left", file=sys.stderr, flush=True)
                continue
            # Bounded, so a hang in setUpClass or in C code (where no test
            # timer can act) ends; at least 90 s, because the recorded seconds
            # leave out class setup: 2haz used 27 s of CPU for 9 s recorded
            # and a loaded run killed it at 10 s + 3x recorded = 38 s.
            # A few seconds per slot stay for the jobs still queued (at most
            # half the time left), so two stalled setups cannot hold both slots
            # until the deadline, nor a long queue starve the job worth most.
            reserve = min(remaining / 2, 3.0 * len(pending) / PARALLEL_CHILDREN)
            budget = min(
                remaining,
                max(10.0, remaining - reserve),
                max(90.0, 10.0 + 3.0 * sum(test["seconds"] for test in job.tests)),
            )
            try:
                child = os.fork()
            except OSError as error:  # this commit keeps every candidate; try the next
                print(f"{job.commit}: fork failed {error!r}", file=sys.stderr, flush=True)
                continue
            if child == 0:
                _child_main(job, index)
            running[child] = (job, time.monotonic(), budget)
        for child, (job, child_started, budget) in list(running.items()):
            pid, _, usage = os.wait4(child, os.WNOHANG)
            if pid:
                del running[child]
                cpu = usage.ru_utime + usage.ru_stime
                print(f"{job.commit}: child done in {time.monotonic() - child_started:.1f} s, {cpu:.2f} s CPU", file=sys.stderr, flush=True)
            elif time.monotonic() - child_started > budget:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                del running[child]
                print(f"{job.commit}: killed after {budget:.0f} s", file=sys.stderr, flush=True)
        # CPU counts toward the score and wall time does not, so poll slowly.
        time.sleep(0.05)


def _child_main(job: Job, index) -> None:
    """Runs in a forked child; never returns."""
    code = 0
    try:
        try:
            # The kernel should pick this child, not the selector, if memory runs out.
            with open("/proc/self/oom_score_adj", "w") as handle:
                handle.write("1000")
        except OSError as error:
            print(f"{job.commit}: oom_score_adj not set: {error!r}", file=sys.stderr, flush=True)
        result = _run_job(job, index)
        target = RESULTS / f"{job.commit}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(result), "utf-8")
        os.replace(temporary, target)
    except BaseException as error:  # the parent reads a missing result as no answer
        print(f"{job.commit}: child failed {error!r}", file=sys.stderr, flush=True)
        code = 1
    finally:
        os._exit(code)


def verify(jobs: list[Job], deadline: float) -> dict[str, dict]:
    """Run the jobs; returns commit -> result for the commits that finished."""
    if not jobs:
        return {}
    RESULTS.mkdir(parents=True, exist_ok=True)
    worker = os.fork()
    if worker == 0:
        code = 0
        try:
            _warm_worker(jobs, deadline)
        except BaseException as error:  # the parent keeps every candidate for unanswered commits
            print(f"warm worker failed: {error!r}", file=sys.stderr, flush=True)
            code = 1
        finally:
            os._exit(code)
    while True:
        pid, status = os.waitpid(worker, os.WNOHANG)
        if pid:
            break
        if time.monotonic() > deadline + 10:
            os.kill(worker, signal.SIGKILL)
            os.waitpid(worker, 0)
            print("warm worker killed at the deadline", file=sys.stderr, flush=True)
            break
        time.sleep(0.25)
    results = {}
    for job in jobs:
        path = RESULTS / f"{job.commit}.json"
        if path.exists():
            try:
                results[job.commit] = json.loads(path.read_text("utf-8"))
            except ValueError as error:
                print(f"{job.commit}: unreadable result {error!r}", file=sys.stderr, flush=True)
    return results
