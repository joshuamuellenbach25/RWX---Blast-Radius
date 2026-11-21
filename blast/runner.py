"""Run sqlglot tests inside the selector's own processes.

Used in forked children only. Two ways to put a patch into effect:
- swap_functions: replace the code of live function objects, for changes
  inside function bodies (the process already imported sqlglot);
- PatchedSourceFinder: serve the patched text of whole modules, for a
  process that has not imported sqlglot yet.
run_tests runs unittest cases in this process with the in-process pool, a
time limit per test, and returns the numbers of the tests that failed.
"""

from __future__ import annotations

import gc
import importlib.abc
import importlib.machinery
import signal
import sys
import types
import unittest

SQLGLOT_ROOT = "/sqlglot/"


# ---------------------------------------------------------------- swapping

def function_index() -> dict[tuple[str, str, int], list[types.FunctionType]]:
    """Every live function compiled from a file under /sqlglot, by
    (filename, qualname, first line)."""
    index: dict[tuple[str, str, int], list[types.FunctionType]] = {}
    for value in gc.get_objects():
        if type(value) is types.FunctionType:
            code = value.__code__
            if code.co_filename.startswith(SQLGLOT_ROOT):
                index.setdefault((code.co_filename, code.co_qualname, code.co_firstlineno), []).append(value)
    return index


def swap_functions(pairs, index) -> list[int]:
    """pairs: (old code, new code). Point every live function that runs the
    old code at the new code. Returns, per pair, how many functions changed."""
    counts = []
    for old, new in pairs:
        key = (old.co_filename, old.co_qualname, old.co_firstlineno)
        swapped = 0
        for function in index.get(key, []):
            if function.__code__.co_code == old.co_code:
                function.__code__ = new
                swapped += 1
        counts.append(swapped)
    return counts


# ---------------------------------------------------------- patched sources

class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, text: str):
        super().__init__(fullname, path)
        self._text = text

    def get_data(self, path):
        if path == self.path:
            return self._text.encode("utf-8")
        return super().get_data(path)

    def get_code(self, fullname):
        return compile(self._text, self.path, "exec", dont_inherit=True)


class PatchedSourceFinder(importlib.abc.MetaPathFinder):
    """Serve patched text for the given files (absolute paths)."""

    def __init__(self, texts: dict[str, str]):
        self._texts = texts

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin not in self._texts:
            return None
        spec.loader = _PatchedLoader(fullname, spec.origin, self._texts[spec.origin])
        return spec


# ----------------------------------------------------------------- running

class TestTimeout(BaseException):
    """Raised by the alarm when one test runs past its limit."""


def _on_alarm(signum, frame):
    raise TestTimeout()


class _Recorder(unittest.TestResult):
    def __init__(self, limits: dict[str, float]):
        super().__init__()
        self.failed_ids: set[str] = set()
        self.failed_classes: set[str] = set()
        self._limits = limits

    def startTest(self, test):
        super().startTest(test)
        signal.setitimer(signal.ITIMER_REAL, self._limits.get(test.id(), 10.0))

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)

    def _mark(self, test):
        test_id = test.id()
        if isinstance(test, unittest.case._SubTest):
            test_id = test.test_case.id()
        if test_id.startswith(("setUpClass (", "tearDownClass (", "setUpModule (", "tearDownModule (")):
            self.failed_classes.add(test_id.split("(", 1)[1].rstrip(")"))
        else:
            self.failed_ids.add(test_id)

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.failed_ids.add(test.id())

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.failed_ids.add(test.id())


def unittest_id(test: dict) -> str:
    module = test["file"][: -len(".py")].replace("/", ".")
    _, class_name, method = test["id"].split("::")
    return f"{module}.{class_name}.{method}"


def run_tests(tests: list[dict], limits_by_number: dict[int, float]) -> tuple[set[int], dict]:
    """Run the given tests (tests.json entries) here. Returns the numbers
    that failed, errored, timed out or could not be loaded, and a summary."""
    import importlib

    from blast import inline_pool

    by_unittest_id = {unittest_id(test): test for test in tests}
    limits = {unittest_id(test): limits_by_number[test["number"]] for test in tests}
    failed: set[int] = set()
    suite = unittest.TestSuite()
    modules = []
    ordered = sorted(tests, key=lambda test: (test["file"], test["id"].split("::")[1], test["seconds"]))
    for test in ordered:
        module_name = test["file"][: -len(".py")].replace("/", ".")
        _, class_name, method = test["id"].split("::")
        try:
            module = importlib.import_module(module_name)
            suite.addTest(getattr(module, class_name)(method))
            modules.append(module)
        except Exception:  # a test file that cannot be imported fails all its tests
            failed.add(test["number"])
    inline_pool.install(modules)
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    recorder = _Recorder(limits)
    try:
        suite.run(recorder)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    for test_id in recorder.failed_ids:
        if test_id in by_unittest_id:
            failed.add(by_unittest_id[test_id]["number"])
    for class_path in recorder.failed_classes:
        for test_id, test in by_unittest_id.items():
            if test_id.startswith(class_path + "."):
                failed.add(test["number"])
    summary = {"run": recorder.testsRun, "failures": len(recorder.failures), "errors": len(recorder.errors)}
    return failed, summary


def quiet_stdout() -> None:
    """Point file descriptor 1 at stderr, so nothing a test prints can
    reach the answers on stdout."""
    import os

    sys.stdout.flush()
    os.dup2(2, 1)
    sys.stdout = sys.stderr
