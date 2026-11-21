"""Turn one patch into the tests it can affect, using the coverage map."""

from __future__ import annotations

import gzip
import pickle
from dataclasses import dataclass, field
from pathlib import Path

from blast.codediff import diff_python, enclosing_spans, statement_spans
from blast.patch import FilePatch, PatchError, apply_file_patch, parse_patch

# Files pytest itself reads before any test runs: a change can alter every test.
RUNNER_CONFIG = {
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "tests/__init__.py", "tests/helpers.py",
}


class CoverageMap:
    def __init__(self, path: Path):
        data = pickle.loads(gzip.decompress(path.read_bytes()))
        self.test_count: int = data["test_count"]
        self.lines: dict[str, dict[int, int]] = data["lines"]
        self.opens: dict[str, int] = data["opens"]
        self.import_lines: dict[str, set[int]] = data["import_lines"]
        self.module_import_lines: dict[str, dict[str, set[int]]] = data["module_import_lines"]
        self.all_tests = (1 << self.test_count) - 1

    def tests_covering(self, filename: str, lines) -> int:
        per_file = self.lines.get(filename, {})
        mask = 0
        for line in lines:
            mask |= per_file.get(line, 0)
        return mask

    def tests_touching_file(self, filename: str) -> int:
        mask = 0
        for value in self.lines.get(filename, {}).values():
            mask |= value
        return mask

    def executed_at_import(self, filename: str, lines) -> bool:
        line_set = set(lines)
        if line_set & self.import_lines.get(filename, set()):
            return True
        return any(
            line_set & per_module.get(filename, set())
            for per_module in self.module_import_lines.values()
        )


@dataclass
class FileVerdict:
    path: str
    kind: str  # same, function, module, fixture, test_function, other, unknown
    tests: int = 0  # bitmask of candidate tests
    note: str = ""
    swaps: list = field(default_factory=list)  # ChangedCode list for function changes
    new_text: str | None = None


@dataclass
class CommitAnalysis:
    files: list[FileVerdict]

    @property
    def tests(self) -> int:
        mask = 0
        for verdict in self.files:
            mask |= verdict.tests
        return mask

    @property
    def needs_everything(self) -> bool:
        return any(verdict.kind in ("unknown", "module") for verdict in self.files)


def analyze(patch_text: str, source_root: Path, coverage: CoverageMap) -> CommitAnalysis:
    try:
        file_patches = parse_patch(patch_text)
    except PatchError as error:
        return CommitAnalysis([FileVerdict("?", "unknown", coverage.all_tests, str(error))])
    if not file_patches:
        return CommitAnalysis([FileVerdict("?", "unknown", coverage.all_tests, "no files in patch")])
    return CommitAnalysis([analyze_file(patch, source_root, coverage) for patch in file_patches])


def analyze_file(patch: FilePatch, source_root: Path, coverage: CoverageMap) -> FileVerdict:
    path = patch.path
    everything = coverage.all_tests
    if patch.binary:
        return FileVerdict(path, "unknown", everything, "binary patch")
    if not patch.hunks and patch.old_path == patch.new_path:
        return FileVerdict(path, "same", 0, "mode change only")
    if patch.old_path is None or patch.new_path is None or patch.old_path != patch.new_path:
        if path.startswith(("sqlglot/", "tests/")) or path in RUNNER_CONFIG:
            return FileVerdict(path, "unknown", everything, "file added, deleted or renamed")
        return FileVerdict(path, "other", 0, "file outside the package and tests")

    old_file = source_root / path
    try:
        old_text = old_file.read_text("utf-8")
        new_text = apply_file_patch(old_text, patch)
    except (OSError, UnicodeDecodeError, PatchError) as error:
        return FileVerdict(path, "unknown", everything, f"cannot apply: {error}")

    if path in RUNNER_CONFIG:
        return FileVerdict(path, "unknown", everything, "runner configuration", new_text=new_text)

    if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
        return analyze_python(path, old_text, new_text, patch, coverage)

    readers = coverage.opens.get(path, 0)
    if path.startswith("sqlglotc/"):
        readers |= coverage.tests_touching_file("tests/test_sqlglotc.py")
    return FileVerdict(path, "fixture" if readers else "other", readers, new_text=new_text)


def analyze_python(
    path: str, old_text: str, new_text: str, patch: FilePatch, coverage: CoverageMap
) -> FileVerdict:
    everything = coverage.all_tests
    filename = "/sqlglot/" + path
    diff = diff_python(old_text, new_text, filename)
    if diff.same_behavior:
        return FileVerdict(path, "same", 0, "identical bytecode", new_text=new_text)
    if diff.syntax_error:
        return FileVerdict(path, "unknown", everything, "new text does not compile", new_text=new_text)
    if not diff.function_level_only:
        return FileVerdict(path, "module", everything, "module or class level change", new_text=new_text)

    changed_lines = patch.changed_old_lines()
    own_lines: set[int] = set()
    for change in diff.changed:
        nested = {
            line
            for const in change.old.co_consts
            if hasattr(const, "co_lines")
            for _, _, line in const.co_lines()
            if line is not None
        }
        own = {line for _, _, line in change.old.co_lines() if line is not None} - nested
        if own:
            own_lines.update(range(min(own), max(own) + 1))
    relevant = (changed_lines & own_lines) or changed_lines
    spans = enclosing_spans(statement_spans(old_text), relevant)
    span_lines = {line for first, last in spans for line in range(first, last + 1)}
    if coverage.executed_at_import(path, span_lines):
        return FileVerdict(path, "module", everything, "changed code runs at import", new_text=new_text)
    tests = coverage.tests_covering(path, span_lines)
    kind = "test_function" if path.startswith("tests/") else "function"
    return FileVerdict(path, kind, tests, f"spans {spans}", diff.changed, new_text)
