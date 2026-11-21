"""Turn one patch into the tests it can affect, using the coverage map."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import inspect
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from blast.codediff import _never_runs, changed_bindings, diff_python, enclosing_spans, parse_cached, statement_spans
from blast.patch import FilePatch, PatchError, apply_file_patch, parse_patch

# tests/test_typing.py compares the bytecode of the annotators defined in
# sqlglot/typing/ without running them, so line coverage never names it.
INSPECTED_PREFIX = "sqlglot/typing/"
INSPECTING_TEST_FILE = "tests/test_typing.py"

# Files pytest itself reads before any test runs: a change can alter every test.
RUNNER_CONFIG = {
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    ".pytest.ini", "pytest.toml", ".pytest.toml",  # pytest 9 reads these too
    "tests/__init__.py", "tests/helpers.py",
}

# Parsing and compiling a module takes about 90 times its text in memory.
# The largest sqlglot module is about 0.4 MB; a patched module past this size
# lists every test instead of risking the container's memory.
MAX_PYTHON_CHARS = 2_000_000

# The tests run with the checkout's root on sys.path, so a module there takes
# its name from whatever module was imported under that name: a stdlib module,
# or one the code imports (an optional import that failed then succeeds).
_IMPORT_LINE = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)|import[ \t]+([^\n#;]+))", re.M)
# What may follow a module's name in its file name (duckdb.backup.py is no
# module duckdb): this interpreter's suffixes, plus plain extensions.
_MODULE_SUFFIXES = frozenset(importlib.machinery.all_suffixes()) | {".so", ".pyd"}
_checkout_imports: dict[Path, set[str]] = {}


def _imported_names(source_root: Path) -> set[str]:
    """The top-level module names that the package and the tests import."""
    names = _checkout_imports.get(source_root)
    if names is None:
        names = set()
        for folder in ("sqlglot", "tests"):
            for file in (source_root / folder).rglob("*.py"):
                try:
                    text = file.read_text("utf-8", errors="replace")
                except OSError:
                    continue
                for match in _IMPORT_LINE.finditer(text):
                    if match.group(1):
                        names.add(match.group(1))
                        continue
                    for part in match.group(2).split(","):
                        words = part.replace("(", " ").replace(")", " ").split()
                        if words:
                            names.add(words[0].partition(".")[0])
        _checkout_imports[source_root] = names
    return names


def _same_syntax_tree(old_text: str, new_text: str) -> bool:
    """Equal syntax trees and equal first two lines (where a coding
    declaration would change how the file decodes)."""
    if old_text.splitlines()[:2] != new_text.splitlines()[:2]:
        return False
    try:
        return ast.dump(ast.parse(old_text)) == ast.dump(ast.parse(new_text))
    except (SyntaxError, ValueError):
        return False


def _no_regular_module(name: str) -> bool:
    """No installed or standard module has this name: only then does a
    folder of that name become the module (a namespace package)."""
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _shadowed_module(paths, source_root: Path) -> str | None:
    """The import name that a file at the checkout's root takes over, if any:
    X for X.py, X/__init__.py or an extension module X.*.so or X.pyd when X
    is a stdlib module or one the code imports; for any other file in a
    folder X there (a namespace package) when the code imports X and no
    installed or standard module X exists (duckdb/notes.md is no duckdb)."""
    for path in paths:
        head, slash, _ = path.partition("/")
        if slash:
            regular = path == f"{head}/__init__.py"
            name = head
        else:
            stem = path.partition(".")[0]
            regular = path[len(stem):] in _MODULE_SUFFIXES
            name = stem if regular else ""
        if not name.isidentifier() or name in ("sqlglot", "tests", "sqlglotc"):
            continue
        if regular and (name in _imported_names(source_root) or name in sys.stdlib_module_names):
            return name
        if not regular and name in _imported_names(source_root) and _no_regular_module(name):
            return name
    return None


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

    def tests_creating_module(self, module: str) -> int:
        """Tests that created an object of any class the module defines: a
        module that fails to import takes all of its classes with it."""
        prefix = f"{module}:"
        mask = 0
        for key in self.classes._indexes:
            if key.startswith(prefix):
                mask |= self.tests_creating(key)
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
    try:
        top = {entry.name for entry in source_root.iterdir()}
    except OSError:
        top = set()
    verdicts = []
    for patch in file_patches:
        if _strip_unknown_prefix(patch, top):
            verdicts.append(analyze_file(patch, source_root, coverage))
        else:
            verdicts.append(FileVerdict(patch.path, "unknown", coverage.all_tests, "path not in the checkout"))
    return CommitAnalysis(verdicts)


_pristine_docstrings: dict[str, dict[tuple, str]] = {}


def _docstrings(text: str, pristine: bool) -> dict[tuple, str]:
    """The docstring of the module and of every class and function that has
    one, keyed by nesting path plus the definition's place among same-named
    definitions under one owner (a property's getter and setter share a
    name). A definition with no docstring adds nothing, so an added
    undocumented helper changes nothing here. Empty when the text does not
    parse. A pristine text's result is kept for the next commit; a patched
    text is parsed outside the cache of pristine trees, so no patched tree
    is kept."""
    if pristine and text in _pristine_docstrings:
        return _pristine_docstrings[text]
    try:
        tree = parse_cached(text) if pristine else ast.parse(text)
    except SyntaxError:
        return {}
    found: dict[tuple, str] = {}
    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc is not None:
        found[()] = module_doc
    places: dict[tuple, int] = {}
    pending: list[tuple[tuple, ast.AST]] = [((), tree)]
    while pending:
        owner, node = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                path = (*owner, child.name)
                place = places.get(path, 0)
                places[path] = place + 1
                doc = ast.get_docstring(child, clean=False)
                if doc is not None:
                    found[(*path, place)] = doc
                pending.append((path, child))
            elif isinstance(child, ast.stmt):
                pending.append((owner, child))
    if pristine:
        _pristine_docstrings[text] = found
    return found


def _star_names(source_root: Path, text: str) -> set[str] | None:
    """The names a module's star imports bind, from the pinned source: every
    public module-level name of each sqlglot module it star-imports (none
    sets __all__). None when one cannot be read there."""
    if "import *" not in text:
        return None
    try:
        tree = ast.parse(text)  # patched text: not kept in the cache of pristine trees
    except SyntaxError:
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            exported = _module_exports(source_root, node.module or "", node.level, 0) if node in tree.body else None
            if exported is None:
                return None
            names |= exported
    return names


def _module_exports(source_root: Path, module: str, level: int, depth: int) -> set[str] | None:
    """Every name a star import of this module binds: its module-level names
    that do not start with an underscore, those it star-imports included.
    None for a relative import, a module outside sqlglot, one that sets
    __all__, or one that cannot be read."""
    if level or depth > 4 or not (module == "sqlglot" or module.startswith("sqlglot.")):
        return None
    base = source_root / module.replace(".", "/")
    path = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
    try:
        tree = parse_cached(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            inner = _module_exports(source_root, node.module or "", node.level, depth + 1)
            if inner is None:
                return None
            names |= inner
            continue
        names |= _module_scope_names(node)
    if "__all__" in names:
        return None
    return {name for name in names if not name.startswith("_")}


def _module_scope_names(statement: ast.stmt) -> set[str]:
    """The names a module-level statement binds in the module itself: a def
    or class binds its name, not what its body binds (arg_types inside a
    class is no module name), and a lambda or comprehension binds nothing."""
    names: set[str] = set()
    pending: list[ast.AST] = [statement]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, ast.If) and _never_runs(node.test):
            # Only type checkers enter if t.TYPE_CHECKING: (DataTypeArgs is
            # bound there and nowhere at run time); its else branch runs.
            pending.extend(node.orelse)
            continue
        if isinstance(node, ast.Import):
            names |= {alias.asname or alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {alias.asname or alias.name for alias in node.names if alias.name != "*"}
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        pending.extend(ast.iter_child_nodes(node))
    return names


def _strip_unknown_prefix(patch: FilePatch, top: set[str]) -> bool:
    """Strips a path prefix other than a/ and b/ (old/ and new/, say): a
    first folder the pinned checkout lacks in front of one it has. False
    when a changed, deleted or renamed file still starts outside the
    checkout, so its path cannot be read."""
    for attribute in ("old_path", "new_path"):
        path = getattr(patch, attribute)
        if path is None:
            continue
        head, _, rest = path.partition("/")
        if head not in top and rest.partition("/")[0] in top:
            setattr(patch, attribute, rest)
    return patch.old_path is None or patch.old_path.partition("/")[0] in top


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
    # A deleted or renamed file stays in place for a local run, so a commit
    # with one cannot be checked by running its tests here.
    moved = patch.old_path is not None and patch.new_path != patch.old_path
    if not inside:
        shadowed = _shadowed_module(patch.paths, source_root)
        if shadowed:
            return FileVerdict(path, "unknown", everything, f"a file at the checkout's root takes the import name {shadowed}")
    if not inside and not readers and not path.startswith("sqlglotc/"):
        # No test imports, runs or opens this file.
        if moved:
            return FileVerdict(path, "moved", 0, "unread file deleted or renamed")
        return FileVerdict(path, "other", 0, "file no test reads")
    if patch.binary:
        return FileVerdict(path, "unknown", everything, "binary patch")
    if not patch.hunks and patch.old_path == patch.new_path:
        return FileVerdict(path, "same", 0, "mode change only")
    if patch.old_path is None or patch.new_path is None or patch.old_path != patch.new_path:
        if inside:
            return FileVerdict(path, "unknown", everything, "file added, deleted or renamed")
        return FileVerdict(path, "moved" if moved else "fixture", readers, "read file added, deleted or renamed")

    old_file = source_root / path
    try:
        old_text = old_file.read_text("utf-8")
        new_text = apply_file_patch(old_text, patch)
    except (OSError, UnicodeDecodeError, PatchError) as error:
        return FileVerdict(path, "unknown", everything, f"cannot apply: {error}")

    if path in RUNNER_CONFIG or path.rsplit("/", 1)[-1] == "conftest.py":
        # Q2's proof that a change breaks nothing: the same syntax tree (a
        # comment, a blank line), with the first two lines, where a coding
        # declaration sits, unchanged.
        if path.endswith(".py") and _same_syntax_tree(old_text, new_text):
            return FileVerdict(path, "same", 0, "identical syntax tree", new_text=new_text)
        return FileVerdict(path, "unknown", everything, "runner configuration", new_text=new_text)

    if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
        if len(new_text) > MAX_PYTHON_CHARS:
            return FileVerdict(path, "unknown", everything, f"patched module of {len(new_text)} characters, too large to parse", new_text=new_text)
        return analyze_python(path, old_text, new_text, patch, coverage, source_root)
    if old_text == new_text:
        return FileVerdict(path, "same", 0, "text unchanged", new_text=new_text)
    return FileVerdict(path, "fixture" if readers else "other", readers, new_text=new_text)


def analyze_python(
    path: str, old_text: str, new_text: str, patch: FilePatch, coverage: CoverageMap, source_root: Path
) -> FileVerdict:
    everything = coverage.all_tests
    filename = "/sqlglot/" + path
    diff = diff_python(old_text, new_text, filename)
    # A docstring is a value at run time (__doc__) that the compared code
    # does not carry and no swap of function code applies: a package file
    # whose docstrings change is checked in a fresh run (a module verdict),
    # where a test that reads one sees the new text.
    docs_changed = path.startswith("sqlglot/") and _docstrings(old_text, True) != _docstrings(new_text, False)
    if diff.same_behavior:
        if docs_changed:
            return FileVerdict(path, "module", 0, "docstring changed", new_text=new_text)
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
        kind = "test_function" if path.startswith("tests/") else "module" if docs_changed else "function"
        swaps = function_changes + list(diff.ancestors)
        return FileVerdict(path, kind, function_tests, function_note, swaps, new_text)

    if runs_at_import or diff.unmatched:
        return FileVerdict(path, "module", everything, "changed code runs at import", new_text=new_text)
    try:
        bindings = changed_bindings(old_text, new_text, _star_names(source_root, new_text))
    except SyntaxError as error:
        return FileVerdict(path, "module", everything, f"cannot compare statements: {error}", new_text=new_text)
    tests, notes = _binding_candidates(path, bindings, coverage, source_root)
    if path.startswith("tests/"):
        # A test module's own statements set up its tests (a lifecycle method
        # assigned after its class, a shared helper) and those of every test
        # file that imports it.
        for test_file in {path, *_test_files_loading(source_root, path)}:
            tests |= coverage.tests_in_test_file(test_file)
        notes += "; every test of the test files that load it"
    return FileVerdict(
        path, "module", tests | function_tests, f"{notes}; {function_note}", function_changes, new_text
    )


ON_DEMAND_PREFIXES = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/", "sqlglot/typing/")
# The files under those folders that `import sqlglot` loads at the pinned
# commit (measured from sys.modules after the import).
LOADED_WITH_SQLGLOT = {
    "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py", "sqlglot/parsers/__init__.py",
    "sqlglot/parsers/base.py", "sqlglot/typing/__init__.py",
}
# Unindented imports run when their file loads; indented ones run later or never.
_IMPORT = re.compile(r"^import ((?:sqlglot|tests)(?:\.\w+)*)", re.M)
_FROM = re.compile(r"^from ((?:sqlglot|tests)(?:\.\w+)*) import (?:\(([^)]*)\)|([\w ,]+))", re.M)
_import_graphs: dict[Path, dict[str, set[str]]] = {}


def _loaded_on_demand(path: str) -> bool:
    """Dialect parts load when a dialect is first used; everything else under
    sqlglot loads with `import sqlglot`."""
    return path.startswith(ON_DEMAND_PREFIXES) and path not in LOADED_WITH_SQLGLOT and not path.endswith("/__init__.py")


_module_file_cache: dict[tuple[Path, str], list[str]] = {}


def _module_files(source_root: Path, module: str) -> list[str]:
    """The files that importing `module` runs: each package's __init__.py on
    the way down, then the module itself."""
    cached = _module_file_cache.get((source_root, module))
    if cached is not None:
        return cached
    parts = module.split(".")
    files = []
    for depth in range(1, len(parts) + 1):
        base = "/".join(parts[:depth])
        for candidate in (f"{base}/__init__.py", f"{base}.py"):
            if (source_root / candidate).is_file():
                files.append(candidate)
                break
    _module_file_cache[(source_root, module)] = files
    return files


def _top_level_imports(source_root: Path, rel: str) -> set[str]:
    """The sqlglot and test files that `rel` runs as it loads."""
    text = (source_root / rel).read_text("utf-8")
    found: set[str] = set()
    for match in _IMPORT.finditer(text):
        found.update(_module_files(source_root, match.group(1)))
    for match in _FROM.finditer(text):
        module = match.group(1)
        found.update(_module_files(source_root, module))
        for name in re.split(r"[\s,]+", match.group(2) or match.group(3)):
            if not name or name == "as":
                continue
            # A name can be a submodule; sqlglot.dialects also loads a
            # dialect's module on first access to its class name.
            found.update(_module_files(source_root, f"{module}.{name}"))
            if module == "sqlglot.dialects":
                found.update(_module_files(source_root, f"{module}.{name.lower()}"))
    return found


def _test_files_loading(source_root: Path, path: str) -> list[str]:
    """Test files that load `path` while they load, directly or through the
    modules they import (33 dialect test files import
    tests/dialects/test_dialect.py, which imports five dialects; the executor
    imports the Python generator). If `path` raises on import, every test in
    these files fails."""
    return sorted(rel for rel in _files_loading(source_root, path) if rel.startswith("tests/"))


IMPORT_GRAPH_PATH = Path(__file__).resolve().parent.parent / "data" / "imports.json"


def build_import_graph(source_root: Path) -> dict[str, set[str]]:
    """file -> the sqlglot and test files it imports as it loads, for every
    .py file under sqlglot/ and tests/ (tools/build_imports.py stores it)."""
    tests_dir = source_root / "tests"
    if not tests_dir.is_dir():
        raise FileNotFoundError(f"no test folder at {tests_dir}")
    graph: dict[str, set[str]] = {}
    for folder in ("sqlglot", "tests"):
        for file in sorted((source_root / folder).rglob("*.py")):
            rel = file.relative_to(source_root).as_posix()
            graph[rel] = _top_level_imports(source_root, rel)
    return graph


def _stored_import_graph() -> dict[str, set[str]] | None:
    """The graph built from the pinned sqlglot when the map was made: reading
    250 files costs about half a second of CPU each run."""
    try:
        data = json.loads(IMPORT_GRAPH_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("format") != "blast-radius-imports/1":
        return None
    return {rel: set(imports) for rel, imports in data["graph"].items()}


def _files_loading(source_root: Path, path: str) -> set[str]:
    """Every sqlglot and test file that runs `path` while it loads, through
    any chain of top-level imports, `path` included: if `path` raises on
    import, each of them fails to load too."""
    graph = _import_graphs.get(source_root)
    if graph is None:
        graph = _stored_import_graph()
        if graph is None or path not in graph:
            graph = build_import_graph(source_root)
        _import_graphs[source_root] = graph
    importers: dict[str, set[str]] = {}
    for rel, imports in graph.items():
        for target in imports:
            importers.setdefault(target, set()).add(rel)
    seen, stack = {path}, [path]
    while stack:
        for parent in importers.get(stack.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _top_level_names(source_root: Path, path: str) -> set[str]:
    """Classes and functions the pristine module defines at top level. Shared
    constant names (EXPRESSION_METADATA in every typing module) would pull in
    the readers of other modules' values, so constants stay out."""
    tree = parse_cached((source_root / path).read_text("utf-8"))
    return {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


def _module_name(path: str) -> str:
    module = path[: -len(".py")].replace("/", ".")
    return module[: -len(".__init__")] if module.endswith(".__init__") else module


def _binding_candidates(path: str, bindings, coverage: CoverageMap, source_root: Path) -> tuple[int, str]:
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
        if binding.kind in ("class-call", "import-risk"):
            # A changed statement that can raise at import (a class-body call,
            # an unbound name, a new operation such as 1 / 0) breaks every test
            # that loads this file. A core module loads with sqlglot itself,
            # so that is every test. A dialect part or a test module loads on
            # demand: the tests that create any class it defines (a dialect
            # module holds little runtime code of its own), the ones that ran
            # its code, and every test in a test file that imports it.
            where = ".".join(binding.owner) or "module level"
            what = "class-body call" if binding.name == "<call>" else binding.name
            if not (_loaded_on_demand(path) or path.startswith("tests/")):
                return everything, f"{where}: {what} in a module sqlglot imports"
            # Every module that imports this one fails to load with it: the
            # tests that create any of their classes, and every test of the
            # test files among them.
            loaders = coverage.tests_touching_file(path)
            for rel in _files_loading(source_root, path):
                if rel.startswith("tests/"):
                    loaders |= coverage.tests_in_test_file(rel)
                else:
                    loaders |= coverage.tests_creating_module(_module_name(rel))
            # A test that imports the module inside its own body (from
            # sqlglot.dialects.athena import Athena) reads its names at run time.
            for top_name in _top_level_names(source_root, path):
                loaders |= coverage.names.get(top_name, 0)
            if not loaders:
                return everything, f"{where}: {what}, no test known to load it"
            tests |= loaders
            notes.append(f"{where}: {what}, all creators and file tests")
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
            # The class is created at import, before any test: a value read
            # at import can break whatever module reads it, so every test.
            if read_at_import:
                return everything, f"{owner_key}.{name}: read at import, class created before any test"
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


_LITERAL_START = re.compile(r"""^[rRbBuUfF]{0,2}['"]""")
_BRANCH = re.compile(r"elif\b|else\s*:")
_HANDLER = re.compile(r"except\b|finally\s*:")
# Flags that change what a call returns or accepts: a return made a yield
# turns the function into a generator, and every caller gets one.
RESHAPING_FLAGS = (
    inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
    | inspect.CO_ITERABLE_COROUTINE | inspect.CO_VARARGS | inspect.CO_VARKEYWORDS
)


def _inert_old_lines(patch, old_text: str) -> set[int]:
    """Old lines the patch removes that run nothing: bare constant statements
    such as docstrings. The file is parsed only when a removed line starts a
    string literal."""
    if not any(
        line[:1] == "-" and _LITERAL_START.match(line[1:].strip())
        for hunk in patch.hunks
        for line in hunk.lines
    ):
        return set()
    inert: set[int] = set()
    for node in ast.walk(parse_cached(old_text)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            inert.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return inert


def _insertion_points(patch, inert: set[int] | frozenset[int] = frozenset()) -> set[int]:
    """Old line numbers where the patch adds code that can run on paths the
    old lines there never ran on: a run of only added lines, or a run whose
    added lines sit left of its removed ones (a statement moved out of its
    block now runs whenever the enclosing code does)."""
    points: set[int] = set()
    for hunk in patch.hunks:
        old_line = hunk.old_start
        start, in_run, removed, added = old_line, False, False, False
        removed_indent = added_indent = None
        removed_branches = added_branches = 0
        removed_handlers: list[str] = []
        added_handlers: list[str] = []
        for line in [*hunk.lines, " "]:  # the sentinel closes the last run
            tag = line[:1]
            if tag == "\\":
                continue
            if tag in "-+":
                if not in_run:
                    start, in_run = old_line, True
                # Blank, comment and docstring lines run nothing: one of them
                # replaced by a statement is an insertion.
                text = line[1:]
                code = text.strip() and not text.strip().startswith("#")
                indent = len(text) - len(text.lstrip(" \t")) if code else None
                branch = bool(indent is not None and _BRANCH.match(text.strip()))
                handler = text.strip() if indent is not None and _HANDLER.match(text.strip()) else None
                if tag == "-":
                    if indent is not None and old_line not in inert:
                        removed = True
                        removed_indent = indent if removed_indent is None else min(removed_indent, indent)
                        removed_branches += branch
                        removed_handlers += [handler] if handler else []
                    old_line += 1
                elif indent is not None:
                    added = True
                    added_indent = indent if added_indent is None else min(added_indent, indent)
                    added_branches += branch
                    added_handlers += [handler] if handler else []
                continue
            dedented = removed_indent is not None and added_indent is not None and added_indent < removed_indent
            # An elif made an if (or an else removed) now runs on the paths
            # the branches before it took, which its old line never saw.
            unchained = added_branches < removed_branches
            # A changed except or finally header changes when its block runs
            # (except made finally runs it on every call); the old header
            # line only ran on the paths that raised.
            rehandled = sorted(removed_handlers) != sorted(added_handlers)
            if added and (not removed or dedented or unchained or rehandled):
                points.add(start)
            in_run = removed = added = False
            removed_indent = added_indent = None
            removed_branches = added_branches = 0
            removed_handlers, added_handlers = [], []
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


def _added_new_lines(patch) -> set[int]:
    """New-side line numbers of the lines the patch adds."""
    added: set[int] = set()
    for hunk in patch.hunks:
        new_line = hunk.new_start
        for line in hunk.lines:
            tag = line[:1]
            if tag == "+":
                added.add(new_line)
            if tag in " +":
                new_line += 1
    return added


def _function_candidates(path, old_text, patch, changes, coverage) -> tuple[int, str, bool]:
    if not changes:
        return 0, "", False
    changed_lines = patch.changed_old_lines()
    insertions = _insertion_points(patch, _inert_old_lines(patch, old_text))
    added_new = _added_new_lines(patch)
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
        # A name the old code read as a global or builtin that the new code
        # binds locally (`int := None`) makes every earlier read of it raise.
        new_locals = set(change.new.co_varnames) | set(change.new.co_cellvars)
        old_locals = set(change.old.co_varnames) | set(change.old.co_cellvars)
        reshaped = (
            (change.old.co_flags ^ change.new.co_flags) & RESHAPING_FLAGS
            or change.old.co_argcount != change.new.co_argcount
            or change.old.co_posonlyargcount != change.new.co_posonlyargcount
            or change.old.co_kwonlyargcount != change.new.co_kwonlyargcount
            or (new_locals - old_locals) & set(change.old.co_names)
        )
        # Lines added to this function where none of its old lines changed
        # (after its last statement and a blank line, say) sit outside its
        # old lines, so no insertion point above falls in them.
        new_nested = {
            line
            for const in change.new.co_consts
            if hasattr(const, "co_lines")
            for _, _, line in const.co_lines()
            if line is not None
        }
        new_own = {line for _, _, line in change.new.co_lines() if line is not None} - new_nested
        appended = bool(new_own & added_new) and not (changed_lines & own)
        if own and (reshaped or appended or any(min(own) <= point <= max(own) + 1 for point in insertions)):
            if tree is None:
                tree = parse_cached(old_text)
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
