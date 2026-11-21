"""Turn one patch into the tests it can affect, using the coverage map."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from blast.codediff import changed_bindings, diff_python, enclosing_spans, statement_spans
from blast.patch import FilePatch, PatchError, apply_file_patch, parse_patch

# tests/test_typing.py compares the bytecode of the annotators defined in
# sqlglot/typing/ without running them, so line coverage never names it.
INSPECTED_PREFIX = "sqlglot/typing/"
INSPECTING_TEST_FILE = "tests/test_typing.py"

# Files pytest itself reads before any test runs: a change can alter every test.
RUNNER_CONFIG = {
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "tests/__init__.py", "tests/helpers.py",
}


class _MaskTable:
    """key -> bitmask of tests, decoded from the map's shared sets on use."""

    def __init__(self, indexes: dict[str, int], coverage: "CoverageMap"):
        self._indexes = indexes
        self._coverage = coverage

    def get(self, key: str, default: int = 0) -> int:
        index = self._indexes.get(key)
        return default if index is None else self._coverage.mask(index)


class CoverageMap:
    """The per-test coverage of the pinned sqlglot commit (tools/build_map.py).
    Tests are bitmasks over the map's own test numbering."""

    def __init__(self, path: Path):
        data = json.loads(path.read_text("utf-8"))
        if data.get("format") != "blast-radius-map/1":
            raise ValueError(f"unknown map format {data.get('format')!r} in {path}")
        self.test_count: int = data["test_count"]
        self.test_ids: list[str] = data["test_ids"]
        self._sets: list[list[int]] = data["sets"]
        self._masks: dict[int, int] = {}
        self._flat_lines: dict[str, list[int]] = data["lines"]
        self._lines: dict[str, dict[int, int]] = {}
        self.opens = _MaskTable(data["opens"], self)
        self.classes = _MaskTable(data["classes"], self)
        self.names = _MaskTable(data["names"], self)
        self.import_lines: dict[str, list[int]] = data["import_lines"]
        self.import_names: set[str] = set(data["import_names"])
        self.import_code: set[tuple[str, str, int]] = {tuple(entry) for entry in data["import_code"]}
        self._bases: dict[str, list[str]] = data["bases"]
        self._children: dict[str, list[str]] | None = None
        self.cpu: list[float] = data["cpu"]
        self.peak_mb: list[int] = data["peak_mb"]
        self.all_tests = (1 << self.test_count) - 1

    def mask(self, index: int) -> int:
        mask = self._masks.get(index)
        if mask is None:
            flat = self._sets[index]
            mask = 0
            for position in range(0, len(flat), 2):
                first, last = flat[position], flat[position + 1]
                mask |= ((1 << (last - first + 1)) - 1) << first
            self._masks[index] = mask
        return mask

    def _file_lines(self, filename: str) -> dict[int, int]:
        per_file = self._lines.get(filename)
        if per_file is None:
            flat = self._flat_lines.get(filename, [])
            per_file = {flat[position]: flat[position + 1] for position in range(0, len(flat), 2)}
            self._lines[filename] = per_file
        return per_file

    def tests_creating(self, class_key: str) -> int:
        """Tests that created an object of the class or of any subclass."""
        if self._children is None:
            children: dict[str, list[str]] = {}
            for child, parents in self._bases.items():
                for parent in parents:
                    children.setdefault(parent, []).append(child)
            self._children = children
        mask = 0
        seen = set()
        stack = [class_key]
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            mask |= self.classes.get(key, 0)
            stack.extend(self._children.get(key, []))
        return mask

    def tests_covering(self, filename: str, lines) -> int:
        per_file = self._file_lines(filename)
        mask = 0
        for line in lines:
            index = per_file.get(line)
            if index is not None:
                mask |= self.mask(index)
        return mask

    def tests_in_test_file(self, test_file: str) -> int:
        """Every test defined in the given test file (tests/....py)."""
        mask = 0
        for number, test_id in enumerate(self.test_ids):
            if test_id.startswith(test_file + "::"):
                mask |= 1 << number
        return mask

    def tests_touching_file(self, filename: str) -> int:
        mask = 0
        for index in set(self._file_lines(filename).values()):
            mask |= self.mask(index)
        return mask

    def executed_at_import(self, filename: str, lines) -> bool:
        return bool(set(lines) & set(self.import_lines.get(filename, [])))


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
    inside = any(
        candidate.startswith(("sqlglot/", "tests/"))
        or candidate in RUNNER_CONFIG
        or candidate.rsplit("/", 1)[-1] == "conftest.py"
        for candidate in patch.paths
    )
    readers = 0
    for candidate in patch.paths:
        readers |= coverage.opens.get(candidate, 0)
    if path.startswith("sqlglotc/"):
        readers |= coverage.tests_touching_file("tests/test_sqlglotc.py")
    if not inside and not readers and not path.startswith("sqlglotc/"):
        # No test imports, runs or opens this file.
        return FileVerdict(path, "other", 0, "file no test reads")
    if patch.binary:
        return FileVerdict(path, "unknown", everything, "binary patch")
    if not patch.hunks and patch.old_path == patch.new_path:
        return FileVerdict(path, "same", 0, "mode change only")
    if patch.old_path is None or patch.new_path is None or patch.old_path != patch.new_path:
        if inside:
            return FileVerdict(path, "unknown", everything, "file added, deleted or renamed")
        return FileVerdict(path, "fixture", readers, "read file added, deleted or renamed")

    old_file = source_root / path
    try:
        old_text = old_file.read_text("utf-8")
        new_text = apply_file_patch(old_text, patch)
    except (OSError, UnicodeDecodeError, PatchError) as error:
        return FileVerdict(path, "unknown", everything, f"cannot apply: {error}")

    if path in RUNNER_CONFIG or path.rsplit("/", 1)[-1] == "conftest.py":
        return FileVerdict(path, "unknown", everything, "runner configuration", new_text=new_text)

    if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
        return analyze_python(path, old_text, new_text, patch, coverage)
    if old_text == new_text:
        return FileVerdict(path, "same", 0, "text unchanged", new_text=new_text)
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
    function_changes = [change for change in diff.changed if change.swappable]
    function_tests, function_note, runs_at_import = _function_candidates(
        path, old_text, patch, function_changes, coverage
    )
    if path.startswith(INSPECTED_PREFIX):
        # test_typing compares the bytecode of these modules' annotators
        # without running them, so line coverage never names it.
        function_tests |= coverage.tests_in_test_file(INSPECTING_TEST_FILE)
    if diff.function_level_only:
        if runs_at_import:
            return FileVerdict(path, "module", everything, "changed code runs at import", new_text=new_text)
        kind = "test_function" if path.startswith("tests/") else "function"
        swaps = function_changes + list(diff.ancestors)
        return FileVerdict(path, kind, function_tests, function_note, swaps, new_text)

    if runs_at_import or diff.unmatched:
        return FileVerdict(path, "module", everything, "changed code runs at import", new_text=new_text)
    try:
        bindings = changed_bindings(old_text, new_text)
    except SyntaxError as error:
        return FileVerdict(path, "module", everything, f"cannot compare statements: {error}", new_text=new_text)
    tests, notes = _binding_candidates(path, bindings, coverage)
    return FileVerdict(
        path, "module", tests | function_tests, f"{notes}; {function_note}", function_changes, new_text
    )


def _module_name(path: str) -> str:
    module = path[: -len(".py")].replace("/", ".")
    return module[: -len(".__init__")] if module.endswith(".__init__") else module


def _binding_candidates(path: str, bindings, coverage: CoverageMap) -> tuple[int, str]:
    """Tests that can see a changed module-level or class-level binding."""
    everything = coverage.all_tests
    if not bindings:
        return everything, "no binding found for a module level change"
    module = _module_name(path)
    tests = 0
    notes = []
    for binding in bindings:
        name = binding.name
        if binding.kind == "other":
            return everything, f"unreadable statement near {'.'.join(binding.owner) or 'module'}"
        if binding.kind == "call":
            return everything, "a changed module-level statement calls code at import"
        if binding.kind == "class-call":
            # A class-body call that raises at import breaks every test that
            # loads this file: the ones that create the class (a dialect
            # module holds little runtime code of its own) and the ones that
            # ran some of the file's code. With neither known, list everything.
            loaders = coverage.tests_creating(f"{module}:{'.'.join(binding.owner)}")
            loaders |= coverage.tests_touching_file(path)
            if not loaders:
                return everything, f"{'.'.join(binding.owner)}: class-body call, no test known to load it"
            tests |= loaders
            notes.append(f"{'.'.join(binding.owner)}: class-body call, all creators and file tests")
            continue
        if binding.kind == "import":
            # An import's effect is the module it loads (dialect classes
            # register themselves when created), not the name it binds.
            return everything, f"import statement changed near {name}"
        if binding.kind == "doc":
            notes.append(f"{'.'.join(binding.owner)}.__doc__")
            continue
        readers = coverage.names.get(name, 0)
        read_at_import = name in coverage.import_names
        body = 0
        if binding.old_def_lines:
            first, last = binding.old_def_lines
            body = coverage.tests_covering(path, range(first, last + 1))
        if not binding.owner:
            if read_at_import and binding.kind != "def":
                return everything, f"module global {name} is read at import"
            tests |= readers | body
            notes.append(f"{name}: readers")
            continue
        owner_key = f"{module}:{'.'.join(binding.owner)}"
        creators = coverage.tests_creating(owner_key)
        if binding.kind == "class":
            creators |= coverage.tests_creating(f"{owner_key}.{name}")
        if not creators:
            if not readers:
                return everything, f"{owner_key}.{name}: no creators and no readers"
            tests |= readers | body
            notes.append(f"{owner_key}.{name}: readers, class never created")
            continue
        if read_at_import or not readers or binding.added or binding.kind == "class":
            tests |= creators | body
            notes.append(f"{owner_key}.{name}: all creators")
        else:
            tests |= (creators & readers) | body
            notes.append(f"{owner_key}.{name}: creators that read it")
    return tests, "; ".join(notes)


def _insertion_points(patch) -> set[int]:
    """Old line numbers before which the patch only adds lines (a run of
    added lines with no removed line in it)."""
    points: set[int] = set()
    for hunk in patch.hunks:
        old_line = hunk.old_start
        start, removed, added = old_line, False, False
        for line in [*hunk.lines, " "]:  # the sentinel closes the last run
            tag = line[:1]
            if tag == "\\":
                continue
            if tag in "-+":
                if not (removed or added):
                    start = old_line
                if tag == "-":
                    removed = True
                    old_line += 1
                else:
                    added = True
                continue
            if added and not removed:
                points.add(start)
            removed = added = False
            old_line += 1
    return points


def _entry_lines(tree: ast.AST, first_line: int) -> list[int]:
    """The first executed statement of the function starting at first_line
    (its def line, or its first decorator's): every call runs it."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        starts = [node.lineno, *(d.lineno for d in node.decorator_list)]
        if first_line not in starts:
            continue
        body = node.body
        if len(body) > 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        return [body[0].lineno]
    return []


def _function_candidates(path, old_text, patch, changes, coverage) -> tuple[int, str, bool]:
    if not changes:
        return 0, "", False
    changed_lines = patch.changed_old_lines()
    insertions = _insertion_points(patch)
    tree = None
    own_lines: set[int] = set()
    fallback = 0
    for change in changes:
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
        # Lines inserted into a function run on whatever path reaches their
        # spot, which the neighboring lines' tests can miss (an insertion
        # after an error branch is reached by every normal call). Such a
        # change also lists every test that called the function.
        if own and any(min(own) <= point <= max(own) + 1 for point in insertions):
            if tree is None:
                tree = ast.parse(old_text)
            fallback |= coverage.tests_covering(path, _entry_lines(tree, change.old.co_firstlineno))
        # A lambda that shares its line with code run at import is invisible
        # to the map: import-time lines are left out of every test, and a
        # factory such as rename_func records only the few tests that reached
        # its line later. List the tests that create its class, or every test
        # when it belongs to no class.
        if own and coverage.executed_at_import(path, own) and "<lambda>" in change.old.co_qualname:
            owner = change.old.co_qualname.split(".<", 1)[0]
            creators = 0
            if owner and "<" not in owner and owner != change.old.co_qualname:
                creators = coverage.tests_creating(f"{_module_name(path)}:{owner}")
            fallback |= creators or coverage.all_tests
    relevant = (changed_lines & own_lines) or changed_lines
    spans = enclosing_spans(statement_spans(old_text), relevant)
    span_lines = {line for first, last in spans for line in range(first, last + 1)}
    # A changed function that already ran while sqlglot imported has left
    # objects built by its old code, which swapping its code cannot reach.
    runs_at_import = any(
        (path, change.old.co_qualname, change.old.co_firstlineno) in coverage.import_code
        for change in changes
    )
    note = f"spans {spans}" + ("; lambda or insertion fallback" if fallback else "")
    return coverage.tests_covering(path, span_lines) | fallback, note, runs_at_import
