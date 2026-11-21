"""Run each commit's cheap candidate tests against the patched code.

The selector process never imports sqlglot. It forks one warm worker that
imports sqlglot and the needed test modules once; that worker forks one
child per commit, points the changed functions at their new code, runs the
commit's cheap candidate tests and writes the failing tests to /tmp. A
commit whose child crashes, times out or cannot apply its change gets no
result, and the selector then keeps every candidate for it.
"""

from __future__ import annotations

import ast
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
    # Run the tests in a fresh interpreter that loads every source's patched
    # text (blast.fresh), for changes a function swap cannot apply.
    fresh: bool = False


def _exec_fresh(job: Job) -> None:
    """Replaces this forked child with a fresh interpreter running the job
    (blast.fresh); the worker waits on it like any child. Never returns."""
    spec = RESULTS / f"{job.commit}.spec.json"
    spec.write_text(
        json.dumps({
            "commit": job.commit,
            "sources": job.sources,
            "tests": job.tests,
            "limits": {str(test["number"]): _limit(test) for test in job.tests},
            "root": os.getcwd(),
            "result": str(RESULTS / f"{job.commit}.json"),
        }),
        "utf-8",
    )
    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bootstrap = f"import sys; sys.path.insert(0, {app!r}); from blast import fresh; fresh.main(sys.argv[1])"
    os.execv(sys.executable, [sys.executable, "-c", bootstrap, str(spec)])


def _limit(test: dict) -> float:
    return 3.0 + 6.0 * test["seconds"]


RAISED = 3  # exit code of a body run that raised


def _bodies_run(bodies: list) -> int:
    """Runs module bodies in order in fresh module objects in one forked
    child, as an import of each would, each installed (in sys.modules and on
    its package) before the next runs. 0 when all ran, RAISED when one
    raised; anything else (the 10 s alarm, a signal) is no answer."""
    child = os.fork()
    if child == 0:
        code = RAISED
        try:
            # A body that never finishes must not outlive its job: the
            # alarm's default action ends this child after 10 s.
            signal.alarm(10)
            for name, filename, text in bodies:
                module = types.ModuleType(name)
                module.__file__ = filename
                module.__package__ = name if filename.endswith("__init__.py") else name.rpartition(".")[0]
                sys.modules[name] = module
                parent, _, attribute = name.rpartition(".")
                if parent in sys.modules:
                    setattr(sys.modules[parent], attribute, module)
                # dont_inherit: this file's "from __future__ import
                # annotations" must not postpone the body's annotations.
                exec(compile(text, filename, "exec", dont_inherit=True), module.__dict__)
            code = 0
        except BaseException:  # SystemExit too: the import would fail
            pass
        finally:
            os._exit(code)
    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status)


def _imported_modules(text: str) -> set[str]:
    """The modules a text imports: X for import X, and X and X.name for
    from X import name (from sqlglot import tokens imports sqlglot.tokens)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def _dependency_order(sources: list) -> list:
    """The changed modules, a module that imports another changed module
    after that one (in a cycle, the first left)."""
    names = {name for name, _, _ in sources}
    imports = {name: _imported_modules(text) & names for name, _, text in sources}
    done: set[str] = set()
    ordered, pending = [], list(sources)
    while pending:
        pick = next(
            (item for item in pending if imports[item[0]] - {item[0]} <= done),
            pending[0],
        )
        ordered.append(pick)
        done.add(pick[0])
        pending.remove(pick)
    return ordered


def _import_failure(sources: list, root: str = "/sqlglot") -> str | None:
    """The changed modules, when their new bodies raise together where their
    old bodies ran clean, each run as an import would (a metaclass reading a
    table built with an empty key raises IndexError at import, which no
    static reading shows); "" otherwise. All of a commit's changed modules
    run together, so one that reads a name another adds sees it. None when
    the old bodies do not run clean again: this check then proves nothing,
    and the caller imports the patched text in a fresh interpreter instead.
    New bodies that do not finish where the old ones did are named too, as
    no answer."""
    olds, news = [], []
    for name, path, new_text in _dependency_order(sources):
        filename = os.path.join(root, path)
        try:
            with open(filename, encoding="utf-8") as handle:
                olds.append((name, filename, handle.read()))
        except OSError:
            return ""
        news.append((name, filename, new_text))
    if not news:
        return ""
    if _bodies_run(olds) != 0:
        return None
    outcome = _bodies_run(news)
    if outcome == 0:
        return ""
    # A new body that raises fails every importer. One that does not finish
    # (the 10 s alarm, a signal) where the old one did is no answer either:
    # unknown, so every test is listed for it too.
    names = ", ".join(name for name, _, _ in news)
    return names if outcome == RAISED else f"{names} (no answer)"


def _run_job(job: Job, index) -> dict:
    from blast import runner

    failure = _import_failure(job.sources)
    if failure is None:
        _exec_fresh(job)  # never returns
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
                (RESULTS / f"{job.commit}.spec.json").unlink(missing_ok=True)  # a fresh child that died before reading it
                cpu = usage.ru_utime + usage.ru_stime
                print(f"{job.commit}: child done in {time.monotonic() - child_started:.1f} s, {cpu:.2f} s CPU", file=sys.stderr, flush=True)
            elif time.monotonic() - child_started > budget:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                del running[child]
                (RESULTS / f"{job.commit}.spec.json").unlink(missing_ok=True)
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
        if job.fresh:
            _exec_fresh(job)
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
