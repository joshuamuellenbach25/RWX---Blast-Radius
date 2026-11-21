"""Compare the old and new versions of one Python file.

Two compiled versions behave the same when every code object has the same
bytecode, names, constants and flags; line numbers are left out, so a change
that only adds comments or blank lines compares equal. Each changed code
object is either inside a function body (its __code__ can be swapped in a
live process) or at module or class level (the module has to be re-imported).
"""

from __future__ import annotations

import ast
import builtins
import dis
import inspect
import re
from collections import Counter
from dataclasses import dataclass, field
from types import CodeType


@dataclass
class ChangedCode:
    path: tuple
    old: CodeType
    new: CodeType

    @property
    def is_function(self) -> bool:
        return bool(self.old.co_flags & inspect.CO_OPTIMIZED)

    @property
    def swappable(self) -> bool:
        # A decorator can read a function's signature once, at import (for
        # example inspect.signature in sqlglot/executor/env.py), so a changed
        # signature is not a body-only change.
        return (
            self.is_function
            and self.old.co_freevars == self.new.co_freevars
            and _signature(self.old) == _signature(self.new)
        )


def _signature(code: CodeType) -> tuple:
    count = code.co_argcount + code.co_kwonlyargcount
    flags = code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS)
    count += bool(flags & inspect.CO_VARARGS) + bool(flags & inspect.CO_VARKEYWORDS)
    return (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, flags, code.co_varnames[:count])


@dataclass
class FileDiff:
    syntax_error: bool = False
    changed: list[ChangedCode] = field(default_factory=list)
    unmatched: list[tuple] = field(default_factory=list)  # code objects added or removed
    # Enclosing functions of changed nested code: a closure made after the
    # swap is built from its parent's constants, so the parent swaps too.
    ancestors: list[ChangedCode] = field(default_factory=list)

    @property
    def same_behavior(self) -> bool:
        return not self.syntax_error and not self.changed and not self.unmatched

    @property
    def function_level_only(self) -> bool:
        return (
            not self.syntax_error
            and not self.unmatched
            and all(change.swappable for change in self.changed)
            and all(change.swappable for change in self.ancestors)
        )


def _const_key(value):
    if isinstance(value, CodeType):
        return ("<code>", value.co_qualname)
    if isinstance(value, (tuple, frozenset)):
        items = [_const_key(item) for item in value]
        if isinstance(value, frozenset):
            items.sort(key=repr)
        return (type(value).__name__, tuple(items))
    # 1 == 1.0 == True, so the type is part of the key.
    return (type(value).__name__, repr(value))


# Class bodies store their own first line number and the attribute names
# their methods set. Both are introspection metadata: a comment inserted above
# a class shifts the first, and neither changes what the code computes.
LINE_METADATA_NAMES = {"__firstlineno__", "__static_attributes__"}


def _instruction_keys(code: CodeType) -> tuple:
    instructions = list(dis.get_instructions(code))
    keys = []
    for index, instruction in enumerate(instructions):
        following = instructions[index + 1] if index + 1 < len(instructions) else None
        if (
            following is not None
            and following.opname == "STORE_NAME"
            and following.argval in LINE_METADATA_NAMES
        ):
            keys.append((instruction.opname, "<metadata>"))
            continue
        argval = instruction.argval
        if isinstance(argval, CodeType):
            argval = ("<code>", argval.co_qualname)
        elif instruction.opcode in dis.hasjump or instruction.opcode in dis.hasexc:
            argval = ("<offset>", argval)
        else:
            argval = _const_key(argval)
        keys.append((instruction.opname, instruction.arg if argval is None else None, argval))
    return tuple(keys)


def fingerprint(code: CodeType) -> tuple:
    return (
        _instruction_keys(code),
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_flags,
        code.co_exceptiontable,
    )


def same_raw(old: CodeType, new: CodeType) -> bool:
    """A cheap test that two code objects fingerprint the same: equal raw
    bytecode and equal tables. Line numbers live outside co_code, so most
    unchanged code passes here without being disassembled; any difference
    falls back to the full fingerprint."""
    return (
        old.co_code == new.co_code
        and old.co_names == new.co_names
        and old.co_varnames == new.co_varnames
        and old.co_freevars == new.co_freevars
        and old.co_cellvars == new.co_cellvars
        and old.co_argcount == new.co_argcount
        and old.co_posonlyargcount == new.co_posonlyargcount
        and old.co_kwonlyargcount == new.co_kwonlyargcount
        and old.co_flags == new.co_flags
        and old.co_exceptiontable == new.co_exceptiontable
        and len(old.co_consts) == len(new.co_consts)
        and all(_const_key(a) == _const_key(b) for a, b in zip(old.co_consts, new.co_consts))
    )


def code_tree(code: CodeType, parents: dict | None = None) -> dict[tuple, CodeType]:
    """Every code object under `code`, keyed by its qualname and its position
    among siblings with the same qualname. Fills `parents` (key -> parent key)
    when given."""
    tree: dict[tuple, CodeType] = {}

    def visit(node: CodeType, parent_key) -> None:
        base = node.co_qualname
        index = 0
        while (base, index) in tree:
            index += 1
        key = (base, index)
        tree[key] = node
        if parents is not None:
            parents[key] = parent_key
        for const in node.co_consts:
            if isinstance(const, CodeType):
                visit(const, key)

    visit(code, None)
    return tree


def compile_text(text: str, filename: str) -> CodeType:
    return compile(text, filename, "exec", dont_inherit=True, optimize=0)


# Many commits patch the same pristine file: compile and parse it once.
# Code objects and syntax trees are only read, never changed.
_compiled: dict[tuple[str, str], CodeType] = {}
_parsed: dict[str, ast.Module] = {}


def compile_pristine(text: str, filename: str) -> CodeType:
    key = (filename, text)
    code = _compiled.get(key)
    if code is None:
        code = _compiled[key] = compile_text(text, filename)
    return code


def parse_cached(text: str) -> ast.Module:
    tree = _parsed.get(text)
    if tree is None:
        tree = _parsed[text] = ast.parse(text)
    return tree


CODING_DECLARATION = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def _declared_coding(text: str) -> list[str]:
    """PEP 263 lines among the first two: the import system honors them,
    compile() of decoded text does not."""
    return [line for line in text.split("\n")[:2] if CODING_DECLARATION.match(line)]


def diff_python(old_text: str, new_text: str, filename: str) -> FileDiff:
    if _declared_coding(old_text) != _declared_coding(new_text):
        return FileDiff(syntax_error=True)
    old_code = compile_pristine(old_text, filename)
    try:
        new_code = compile_text(new_text, filename)
    except (SyntaxError, ValueError):
        return FileDiff(syntax_error=True)
    parents: dict = {}
    old_tree = code_tree(old_code, parents)
    new_tree = code_tree(new_code)
    result = FileDiff()
    for key in old_tree.keys() | new_tree.keys():
        if key not in old_tree or key not in new_tree:
            result.unmatched.append(key)
            continue
        old_node, new_node = old_tree[key], new_tree[key]
        if not same_raw(old_node, new_node) and fingerprint(old_node) != fingerprint(new_node):
            result.changed.append(ChangedCode(key, old_node, new_node))
    changed_keys = {change.path for change in result.changed}
    ancestor_keys: set = set()
    for change in result.changed:
        parent = parents.get(change.path)
        while parent is not None and old_tree[parent].co_flags & inspect.CO_OPTIMIZED:
            if parent not in changed_keys:
                ancestor_keys.add(parent)
            parent = parents.get(parent)
        # A class body inside a function is rebuilt from that function's
        # constants on every call, so swapping the method alone would not
        # reach the objects tests create; such changes take the module path.
        while parent is not None:
            if old_tree[parent].co_flags & inspect.CO_OPTIMIZED:
                result.unmatched.append(change.path)
                break
            parent = parents.get(parent)
    for key in sorted(ancestor_keys):
        if key in new_tree:
            result.ancestors.append(ChangedCode(key, old_tree[key], new_tree[key]))
        else:
            result.unmatched.append(key)
    return result


@dataclass
class Binding:
    """A name that a changed module-level or class-level statement binds."""

    owner: tuple[str, ...]  # enclosing class names, () for module level
    name: str
    kind: str  # assign, def, class, import, doc, other
    old_def_lines: tuple[int, int] | None = None  # body lines of the old def
    added: bool = False  # the name exists only in the new text


def _signature_key(node: ast.stmt) -> tuple:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts = [ast.dump(item) for item in node.decorator_list]
        parts.append(ast.dump(node.args))
        parts.append(ast.dump(node.returns) if node.returns else "")
        parts.extend(ast.dump(item) for item in getattr(node, "type_params", []))
        return ("def", type(node).__name__, node.name, tuple(parts))
    if isinstance(node, ast.ClassDef):
        parts = [ast.dump(item) for item in (*node.bases, *node.keywords, *node.decorator_list)]
        parts.extend(ast.dump(item) for item in getattr(node, "type_params", []))
        return ("class", node.name, tuple(parts))
    return ("stmt", ast.dump(node))


def _base_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call, ast.Starred)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _bound_names(node: ast.stmt) -> list[tuple[str, str]]:
    """(name, kind) pairs a statement binds or mutates; kind "other" when it
    cannot be read."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [(node.name, "def")]
    if isinstance(node, ast.ClassDef):
        return [(node.name, "class")]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [((alias.asname or alias.name).split(".")[0], "import") for alias in node.names]
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    elif isinstance(node, ast.Expr):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return [("__doc__", "doc")]
        base = _base_name(node.value)
        return [(base, "assign")] if base else [("?", "other")]
    elif isinstance(node, ast.Pass):
        return []
    elif isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
        found: list[tuple[str, str]] = []
        if isinstance(node, ast.For):
            targets = [node.target]
        for name in ("body", "orelse", "finalbody"):
            for child in getattr(node, name, []) or []:
                found.extend(_bound_names(child))
        for handler in getattr(node, "handlers", []) or []:
            for child in handler.body:
                found.extend(_bound_names(child))
        for target in targets:
            for element in ast.walk(target):
                if isinstance(element, ast.Name):
                    found.append((element.id, "assign"))
        return found or [("?", "other")]
    else:
        return [("?", "other")]
    found = []
    for target in targets:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                base = _base_name(element)
                found.append((base, "assign") if base else ("?", "other"))
        else:
            base = _base_name(target)
            found.append((base, "assign") if base else ("?", "other"))
    return found


def _calls_code(node: ast.stmt) -> bool:
    """Whether running this statement at import calls any code: a call can
    import modules or register objects, effects no name binding shows. A def
    runs its decorators and default values; a class its bases and keywords."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts = [*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    elif isinstance(node, ast.ClassDef):
        parts = [*node.bases, *node.keywords, *node.decorator_list]
    else:
        parts = [node]
    return any(isinstance(sub, ast.Call) for part in parts for sub in ast.walk(part))


ALWAYS_BOUND = set(dir(builtins)) | {
    "__file__", "__path__", "__spec__", "__loader__", "__package__", "__builtins__", "__module__", "__qualname__",
}


def _unbound_load(node: ast.stmt, bound: set[str]) -> str | None:
    """A name this simple statement reads when it runs at import that nothing
    has bound yet, so running it raises NameError. Lambda bodies run later,
    and compound statements are not read (None: no claim)."""
    if "*" in bound:  # a star import binds names no statement shows
        return None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts = [*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    elif isinstance(node, ast.ClassDef):
        parts = [*node.bases, *node.keywords, *node.decorator_list]
    elif isinstance(node, ast.AnnAssign):
        parts = [node.value] if node.value is not None else []
    elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Expr)):
        parts = [node.value]
    else:
        return None
    for part in parts:
        pending, loads, local = [part], [], set()
        while pending:
            sub = pending.pop()
            if isinstance(sub, ast.Lambda):
                continue
            if isinstance(sub, ast.comprehension):
                local.update(n.id for n in ast.walk(sub.target) if isinstance(n, ast.Name))
            elif isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                local.add(sub.target.id)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                loads.append(sub.id)
            pending.extend(ast.iter_child_nodes(sub))
        for name in loads:
            if name not in bound and name not in local and name not in ALWAYS_BOUND:
                return name
    return None


def _operations(nodes: list) -> Counter:
    """Operations that can raise, in what simple statements evaluate at
    import (lambda bodies run later; compound statements are not read)."""
    found: Counter = Counter()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = [*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
        elif isinstance(node, ast.ClassDef):
            parts = [*node.bases, *node.keywords, *node.decorator_list]
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr)):
            parts = [node.value] if node.value is not None else []
        else:
            continue
        pending = list(parts)
        while pending:
            sub = pending.pop()
            if isinstance(sub, ast.Lambda):
                continue
            # Lookups count by their whole expression: TokenType.BEGIN made
            # TokenType.BEIGN, or str.BEGIN, keeps the number of lookups and
            # raises.
            if isinstance(sub, (ast.BinOp, ast.UnaryOp)):
                found[f"{type(sub).__name__}.{type(sub.op).__name__}"] += 1
            elif isinstance(sub, ast.Attribute):
                found[f"Attribute.{ast.unparse(sub)}"] += 1
            elif isinstance(sub, ast.Subscript):
                # The receiver counts too: t.Union[...] made int[...] raises.
                found[f"Subscript.{ast.unparse(sub)}"] += 1
            pending.extend(ast.iter_child_nodes(sub))
    return found


def _value_kinds(nodes: list) -> dict[str, str]:
    """name -> the kind of value a simple assignment gives it: the syntax
    node's type, and for a constant the constant's own type."""
    kinds: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        kind = type(value).__name__
        if isinstance(value, ast.Constant):
            kind += f".{type(value.value).__name__}"
        for target in targets:
            if isinstance(target, ast.Name):
                kinds[target.id] = kind
    return kinds


def changed_bindings(old_text: str, new_text: str) -> list[Binding]:
    """Names bound by module-level and class-level statements that differ.
    Function bodies are left out: bytecode comparison covers them."""
    import difflib

    def compare(old_body: list, new_body: list, owner: tuple[str, ...], module_bound: set[str]) -> list[Binding]:
        old_keys = [_signature_key(node) for node in old_body]
        new_keys = [_signature_key(node) for node in new_body]
        matcher = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
        found: list[Binding] = []
        # Names this body has bound so far, in source order: a class body
        # also sees the module's names bound before the class statement.
        bound: set[str] = set()
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            olds, news = old_body[i1:i2], new_body[j1:j2]
            if tag == "equal":
                for old_node, new_node in zip(olds, news):
                    if isinstance(old_node, ast.ClassDef):
                        seen = module_bound if owner else bound
                        found.extend(compare(old_node.body, new_node.body, owner + (old_node.name,), set(seen)))
                    bound.update(name for name, _ in _bound_names(new_node))
                continue
            for node in news:
                # A changed statement that reads an unbound name raises at
                # import, like a call that raises: the module fails to load.
                missing = _unbound_load(node, bound | module_bound)
                if missing:
                    found.append(Binding(owner, f"<unbound {missing}>", "class-call" if owner else "import-risk"))
                bound.update(name for name, _ in _bound_names(node))
            # An operation the old statements did not run (1 / 0, a new
            # subscript or attribute) can raise at import the same way.
            added_operations = _operations(news) - _operations(olds)
            if added_operations:
                label = ", ".join(sorted(added_operations))
                found.append(Binding(owner, f"<new {label}>", "class-call" if owner else "import-risk"))
            # A value whose kind changes (a dict made None) can break the code
            # that reads it at import, such as a metaclass building tables.
            old_kinds, new_kinds = _value_kinds(olds), _value_kinds(news)
            retyped = sorted(name for name in old_kinds.keys() & new_kinds.keys() if old_kinds[name] != new_kinds[name])
            if retyped:
                found.append(Binding(owner, f"<new kind of value {', '.join(retyped)}>", "class-call" if owner else "import-risk"))
            old_names = {}
            for node in olds:
                for name, kind in _bound_names(node):
                    lines = None
                    if kind == "def":
                        lines = (node.body[0].lineno, node.end_lineno or node.body[0].lineno)
                    old_names.setdefault(name, (kind, lines))
            new_names = {}
            for node in news:
                for name, kind in _bound_names(node):
                    new_names.setdefault(name, kind)
            # Module-level code runs once at import; a changed statement there
            # that calls code can change state no name lookup reveals. In a
            # class body the call can raise at import and break every test
            # that loads the file, so those statements add the file's tests.
            if any(_calls_code(node) for node in (*olds, *news)):
                found.append(Binding(owner, "<call>", "class-call" if owner else "call"))
            for name in old_names.keys() | new_names.keys():
                kind, lines = old_names.get(name, (new_names.get(name), None))
                found.append(Binding(owner, name, kind or "other", lines, added=name not in old_names))
        return found

    return compare(parse_cached(old_text).body, ast.parse(new_text).body, (), set())


def statement_spans(source: str) -> list[tuple[int, int]]:
    """(first, last) line of every statement; for a compound statement only
    its header, since its body statements are listed on their own."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(parse_cached(source)):
        if isinstance(node, ast.Lambda):
            # A lambda body runs when the lambda is called, not where the
            # statement holding it runs, so it is its own unit.
            spans.append((node.body.lineno, node.body.end_lineno or node.body.lineno))
            continue
        if not isinstance(node, ast.stmt):
            continue
        last = node.end_lineno or node.lineno
        bodies = [
            getattr(node, name)
            for name in ("body", "orelse", "finalbody", "handlers", "cases")
            if isinstance(getattr(node, name, None), list) and getattr(node, name)
        ]
        if bodies:
            first_child = min(child.lineno for body in bodies for child in body)
            last = max(node.lineno, first_child - 1)
        spans.append((node.lineno, last))
    return spans


def enclosing_spans(spans: list[tuple[int, int]], lines: set[int]) -> list[tuple[int, int]]:
    """For each line, the smallest statement span holding it. A line outside
    every statement (blank or comment) maps to the nearest statements above
    and below it."""
    chosen: set[tuple[int, int]] = set()
    for line in lines:
        holding = [span for span in spans if span[0] <= line <= span[1]]
        if holding:
            chosen.add(min(holding, key=lambda span: span[1] - span[0]))
            continue
        above = [span for span in spans if span[1] < line]
        below = [span for span in spans if span[0] > line]
        if above:
            chosen.add(max(above, key=lambda span: (span[1], -span[0])))
        if below:
            chosen.add(min(below, key=lambda span: (span[0], span[1])))
    return sorted(chosen)
