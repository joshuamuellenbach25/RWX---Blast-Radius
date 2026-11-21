"""Run each commit's cheap candidate tests against the patched code.

The selector process never imports sqlglot. It forks one warm worker that
imports sqlglot and the needed test modules once; that worker forks one
child per commit, points the changed functions at their new code, runs the
commit's cheap candidate tests and writes the failing tests to /tmp. A
commit whose child crashes, times out or cannot apply its change gets no
result, and the selector then keeps every candidate for it.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

RESULTS = Path("/tmp/blast-results")


@dataclass
class Job:
    commit: str
    tests: list[dict]  # tests.json entries to run
    pairs: list = field(default_factory=list)  # (old code, new code)
    required: list = field(default_factory=list)  # old codes that must have live functions
    modules: set[str] = field(default_factory=set)  # modules to import before forking


def _limit(test: dict) -> float:
    return 3.0 + 6.0 * test["seconds"]


def _run_job(job: Job, index) -> dict:
    from blast import runner

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
    failed, summary = runner.run_tests(job.tests, limits)
    return {"commit": job.commit, "ok": True, "failed": sorted(failed), "swapped": swapped, "summary": summary}


def _warm_worker(jobs: list[Job], deadline: float) -> None:
    from blast import runner

    runner.quiet_stdout()
    os.chdir("/sqlglot")
    if "/sqlglot" not in sys.path:
        sys.path.insert(0, "/sqlglot")
    started = time.monotonic()
    import sqlglot  # noqa: F401

    for module in sorted({name for job in jobs for name in job.modules}):
        try:
            importlib.import_module(module)
        except Exception as error:  # the child reports the tests of a broken module as failed
            print(f"warm import {module} failed: {error!r}", file=sys.stderr, flush=True)
    index = runner.function_index()
    print(f"warm worker ready in {time.monotonic() - started:.1f} s, {len(index)} functions", file=sys.stderr, flush=True)
    for job in jobs:
        remaining = deadline - time.monotonic()
        if remaining < 5:
            print(f"{job.commit}: no time left", file=sys.stderr, flush=True)
            continue
        budget = min(remaining, 5.0 + sum(_limit(test) for test in job.tests))
        child = os.fork()
        if child == 0:
            code = 0
            try:
                try:
                    with open("/proc/self/oom_score_adj", "w") as handle:
                        handle.write("1000")
                except OSError:
                    pass
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
        child_started = time.monotonic()
        while True:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                break
            if time.monotonic() - child_started > budget:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                print(f"{job.commit}: killed after {budget:.0f} s", file=sys.stderr, flush=True)
                break
            time.sleep(0.02)


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
        time.sleep(0.05)
    results = {}
    for job in jobs:
        path = RESULTS / f"{job.commit}.json"
        if path.exists():
            try:
                results[job.commit] = json.loads(path.read_text("utf-8"))
            except ValueError as error:
                print(f"{job.commit}: unreadable result {error!r}", file=sys.stderr, flush=True)
    return results
