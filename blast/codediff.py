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
    new_parents: dict = {}
    old_tree = code_tree(old_code, parents)
    new_tree = code_tree(new_code, new_parents)
    result = FileDiff()
    added_or_removed = []
    for key in old_tree.keys() | new_tree.keys():
        if key not in old_tree or key not in new_tree:
            added_or_removed.append(key)
            continue
        old_node, new_node = old_tree[key], new_tree[key]
        if not same_raw(old_node, new_node) and fingerprint(old_node) != fingerprint(new_node):
            result.changed.append(ChangedCode(key, old_node, new_node))
    changed_keys = {change.path for change in result.changed}
    for key in added_or_removed:
        # Code added or removed inside a function that changed runs only when
        # that function runs, and its new code carries the nested code: the
        # function's change covers it. Anything else runs at import.
        tree, parent_of = (new_tree, new_parents) if key in new_tree else (old_tree, parents)
        parent = parent_of.get(key)
        while parent is not None and tree[parent].co_flags & inspect.CO_OPTIMIZED and parent not in changed_keys:
            parent = parent_of.get(parent)
        # The covering parent must be a function: a new method's parent is
        # the class body and a new def's is the module, both changed too.
        covered = parent is not None and parent in changed_keys and bool(tree[parent].co_flags & inspect.CO_OPTIMIZED)
        if not covered:
            result.unmatched.append(key)
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


DUNDER = re.compile(r"__\w+__")
# Whether the module being compared evaluates annotations when it runs:
# without "from __future__ import annotations" a def evaluates its argument
# and return annotations, and a module or class level x: T = v evaluates T.
# Set by changed_bindings for the module it compares.
_ANNOTATIONS_RUN = False


def _lazy_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _evaluated_parts(node: ast.stmt) -> list:
    """The expressions a def, class or simple statement evaluates when it
    runs at import, the reads inside its assignment targets included."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts = [*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
        if _ANNOTATIONS_RUN:
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg]
            parts += [argument.annotation for argument in arguments if argument is not None and argument.annotation is not None]
            parts += [node.returns] if node.returns is not None else []
        return parts
    if isinstance(node, ast.ClassDef):
        return [*node.bases, *node.keywords, *node.decorator_list]
    if isinstance(node, ast.Expr):
        return [node.value]
    if isinstance(node, ast.AnnAssign):
        parts = [node.value] if node.value is not None else []
        parts += [node.annotation] if _ANNOTATIONS_RUN else []
        targets = [node.target]
    elif isinstance(node, (ast.Assign, ast.AugAssign)):
        parts = [node.value]
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    else:
        return []
    # A subscript or attribute target reads its object first: pretty[0] =
    # False reads pretty, and raises when nothing bound it.
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Subscript):
                parts += [sub.value, sub.slice]
            elif isinstance(sub, ast.Attribute):
                parts.append(sub.value)
    return parts
ALWAYS_BOUND = set(dir(builtins)) | {
    "__file__", "__path__", "__spec__", "__loader__", "__package__", "__builtins__", "__module__", "__qualname__",
}


def _unbound_body(body: list, bound: set[str]) -> str | None:
    """The first unbound name the statements of one branch read, in order."""
    seen = set(bound)
    for child in body:
        missing = _unbound_load(child, seen)
        if missing:
            return missing
        sure = _sure_binds(child)
        if sure is None:  # the rest of the branch never runs
            return None
        seen |= sure
    return None


def _target_names(target: ast.AST | None) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)} if target is not None else set()


def _never_runs(test: ast.expr) -> bool:
    """An if test that is false when the code runs: a false constant or
    TYPE_CHECKING read from the typing module (t, as sqlglot imports it),
    which only type checkers set. str.TYPE_CHECKING raises instead."""
    if isinstance(test, ast.Constant):
        return not test.value
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING" and isinstance(test.value, ast.Name) and test.value.id in ("t", "typing")
    return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"


def _always_true(test: ast.expr) -> bool:
    return isinstance(test, ast.Constant) and bool(test.value)


def _branches(node: ast.stmt) -> list[list]:
    """The statement lists inside a compound statement that can run."""
    if isinstance(node, ast.If):
        live = [] if _never_runs(node.test) else [node.body]
        return live if _always_true(node.test) else [*live, node.orelse]
    if isinstance(node, ast.Try):
        return [node.body, *(handler.body for handler in node.handlers), node.orelse, node.finalbody]
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return [node.body, node.orelse]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [node.body]
    return []


def _store_targets(node: ast.stmt) -> list:
    """The targets a statement assigns by unpacking or binding."""
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.optional_vars for item in node.items if item.optional_vars is not None]
    return []


def _headers(node: ast.stmt) -> list:
    """The expressions a compound statement evaluates itself."""
    if isinstance(node, (ast.If, ast.While)):
        return [node.test]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.iter]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.context_expr for item in node.items]
    if isinstance(node, ast.Try):
        return [handler.type for handler in node.handlers if handler.type is not None]
    return []


def _sure_binds(node: ast.stmt) -> set[str] | None:
    """Names a statement is sure to have bound when it completes, or None
    when it never completes (it raises). A name bound in one branch of an if
    only is not sure, nor one bound in a loop body that may run no times."""
    if isinstance(node, ast.Raise):
        return None
    if isinstance(node, ast.If):
        sure = [names for names in (_always_binds(branch) for branch in _branches(node)) if names is not None]
        return set.intersection(*sure) if sure else None
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return set()
    if isinstance(node, (ast.With, ast.AsyncWith)):
        inner = _always_binds(node.body)
        return None if inner is None else inner.union(*(_target_names(item.optional_vars) for item in node.items))
    if isinstance(node, ast.Try):
        final = _always_binds(node.finalbody)
        paths = [_always_binds([*node.body, *node.orelse]), *(_always_binds(handler.body) for handler in node.handlers)]
        sure = [names for names in paths if names is not None]
        if final is None or not sure:
            return None
        return set.intersection(*sure) | final
    return _binds(node)


def _always_binds(body: list) -> set[str] | None:
    names: set[str] = set()
    for child in body:
        sure = _sure_binds(child)
        if sure is None:
            return None
        names |= sure
    return names


def _unbound_load(node: ast.stmt, bound: set[str]) -> str | None:
    """A name this statement reads when it runs at import that nothing has
    bound yet, so running it raises NameError. Lambda bodies run later. A
    compound statement is read through its header and each branch; a branch
    starts from the names bound before it (None: no claim)."""
    if "*" in bound:  # a star import binds names no statement shows
        return None
    if isinstance(node, (ast.If, ast.While)):
        # A branch that cannot run (if TYPE_CHECKING:) reads nothing.
        return _unbound_in([node.test], bound) or next(
            (missing for branch in _branches(node) if (missing := _unbound_body(branch, bound))), None
        )
    if isinstance(node, (ast.For, ast.AsyncFor)):
        inner = bound | _target_names(node.target)
        return _unbound_in([node.iter], bound) or _unbound_body(node.body, inner) or _unbound_body(node.orelse, inner)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        inner = bound.union(*(_target_names(item.optional_vars) for item in node.items))
        return _unbound_in([item.context_expr for item in node.items], bound) or _unbound_body(node.body, inner)
    if isinstance(node, ast.Try):
        # A handler can run after part of the body bound its names; counting
        # them as bound avoids claiming a NameError that may not happen.
        after = bound | {name for child in node.body for name in _binds(child)}
        return (
            _unbound_body(node.body, bound)
            or next((m for h in node.handlers if (m := _unbound_in([h.type] if h.type else [], after)
                                                  or _unbound_body(h.body, after | ({h.name} if h.name else set())))), None)
            or _unbound_body(node.orelse, after)
            or _unbound_body(node.finalbody, after)
        )
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.AnnAssign, ast.Assign, ast.AugAssign, ast.Expr)):
        return None
    parts = _evaluated_parts(node)
    # An augmented assignment reads its target first. In a class body an
    # inherited name is not in the class namespace, so that read raises.
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        if node.target.id not in bound and node.target.id not in ALWAYS_BOUND:
            return node.target.id
    return _unbound_in(parts, bound)


def _unbound_in(parts: list, bound: set[str]) -> str | None:
    """The first name these expressions read that nothing has bound."""
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


UNHASHABLE = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)


def _kind(node: ast.AST) -> str:
    """A node's syntax type; for a constant, the constant's own type."""
    if isinstance(node, ast.Constant):
        return f"Constant.{type(node.value).__name__}"
    return type(node).__name__


def _operations(nodes: list) -> Counter:
    """Operations that can raise, in what simple statements evaluate at
    import (lambda bodies run later; compound statements are not read)."""
    found: Counter = Counter()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr)):
            parts = _evaluated_parts(node)
            # Storing through a subscript or attribute can raise (item
            # assignment on a constant); a store already made here cannot.
            targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
            for target in targets:
                for sub in ast.walk(target) if target is not None else ():
                    if isinstance(sub, (ast.Subscript, ast.Attribute)) and isinstance(sub.ctx, ast.Store):
                        found[f"Store.{ast.unparse(sub)}"] = 1
        elif _branches(node) or _headers(node):
            # A compound statement runs its own expressions and the
            # statements of every branch that can run. An if TYPE_CHECKING:
            # header reads a name the typing module always has.
            parts = [] if isinstance(node, ast.If) and _never_runs(node.test) else _headers(node)
            for branch in _branches(node):
                found += _operations(branch)
        else:
            continue
        # A target that unpacks raises when the value has another length or
        # cannot be iterated (pretty, = False).
        for target in _store_targets(node):
            for sub in ast.walk(target):
                if isinstance(sub, (ast.Tuple, ast.List)):
                    starred = any(isinstance(element, ast.Starred) for element in sub.elts)
                    found[f"Unpack{'*' if starred else len(sub.elts)}"] += 1
        pending = list(parts)
        while pending:
            sub = pending.pop()
            if isinstance(sub, ast.Lambda):
                continue
            # Lookups count by their whole expression: TokenType.BEGIN made
            # TokenType.BEIGN, or str.BEGIN, keeps the number of lookups and
            # raises. An operator's operands count by kind: a list plus 1
            # raises TypeError where a list plus a list did not.
            if isinstance(sub, ast.BinOp):
                found[f"BinOp.{type(sub.op).__name__}"] += 1
                for operand in (sub.left, sub.right):
                    found[f"Operand.{_kind(operand)}"] += 1
            elif isinstance(sub, ast.UnaryOp):
                found[f"UnaryOp.{type(sub.op).__name__}"] += 1
            elif isinstance(sub, (ast.Set, ast.Dict)):
                # An unhashable set member or key ([TokenType.BEGIN] in a set)
                # and unpacking a non-mapping ({**True}) raise TypeError.
                members = sub.elts if isinstance(sub, ast.Set) else sub.keys
                for member in members:
                    if member is None:
                        found["DictUnpack"] += 1
                    elif isinstance(member, UNHASHABLE):
                        found["Unhashable"] += 1
            elif isinstance(sub, ast.Starred):
                found["Starred"] += 1
            elif isinstance(sub, ast.Attribute):
                found[f"Attribute.{ast.unparse(sub)}"] += 1
            elif isinstance(sub, ast.Subscript):
                # The receiver counts too: t.Union[...] made int[...] raises.
                found[f"Subscript.{ast.unparse(sub)}"] += 1
            pending.extend(ast.iter_child_nodes(sub))
    # A lookup that already ran here gives the same answer when repeated:
    # one more "SELECTX": TokenType.SELECT entry looks up nothing new.
    for key in found:
        if key.startswith(("Attribute.", "Subscript.")):
            found[key] = 1
    return found


def _binds(node: ast.stmt) -> set[str]:
    """Names a statement binds when it runs: an annotation with no value
    binds nothing."""
    if isinstance(node, ast.AnnAssign) and node.value is None:
        return set()
    return {name for name, _ in _bound_names(node)}


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
        # A signed number (-1) is a constant like 0, not a new kind of value.
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.USub, ast.UAdd)) \
                and isinstance(value.operand, ast.Constant) and isinstance(value.operand.value, (int, float, complex)):
            value = value.operand
        kind = type(value).__name__
        if isinstance(value, ast.Constant):
            kind += f".{type(value.value).__name__}"
        for target in targets:
            if isinstance(target, ast.Name):
                kinds[target.id] = kind
    return kinds


def _tag(node: ast.AST) -> str:
    """A node's kind; a tuple also keeps its length, since code unpacks a
    pair by position."""
    if isinstance(node, ast.Tuple):
        return f"Tuple{len(node.elts)}"
    return _kind(node)


def _shapes(node: ast.AST, prefix: str = "") -> set[str]:
    """The kinds of the elements of a literal container, with the containers
    they sit in: [("x", "y")] gives List>Tuple2 and List>Tuple2>Constant.str.
    Dict keys and values are kept apart. Only a nested tuple keeps its
    length, since the value itself may be a tuple of names that grows."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        groups = [("", list(node.elts))]
    elif isinstance(node, ast.Dict):
        groups = [(".key", [n for n in node.keys if n is not None]), (".value", list(node.values))]
    else:
        return set()
    name = _tag(node) if prefix else type(node).__name__
    found: set[str] = set()
    for suffix, elements in groups:
        here = f"{prefix}{name}{suffix}>"
        for element in elements:
            found.add(here + _tag(element))
            found |= _shapes(element, here)
    return found


def _element_shapes(nodes: list) -> dict[str, set[str]]:
    """name -> the element kinds of the literal container a simple assignment
    gives it."""
    shapes: dict[str, set[str]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                shapes[target.id] = _shapes(value)
    return shapes


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
                    bound.update(_sure_binds(new_node) or set())
                continue
            for node in news:
                # A changed statement that reads an unbound name raises at
                # import, like a call that raises: the module fails to load.
                missing = _unbound_load(node, bound | module_bound)
                if missing:
                    found.append(Binding(owner, f"<unbound {missing}>", "class-call" if owner else "import-risk"))
                bound.update(_sure_binds(node) or set())
            # An operation the old statements did not run (1 / 0, a new
            # subscript or attribute) can raise at import the same way.
            added_operations = _operations(news) - _operations(olds)
            if added_operations:
                label = ", ".join(sorted(added_operations))
                found.append(Binding(owner, f"<new {label}>", "class-call" if owner else "import-risk"))
            # Special names act while the module or class is built: __all__
            # decides what a star import copies, and a bad __slots__ entry
            # stops the class from being created. A docstring does neither.
            # Methods such as __init__ are compared as code, not here.
            changed_names = {
                name for node in (*olds, *news) for name, kind in _bound_names(node) if kind not in ("def", "class", "import", "doc")
            }
            special = sorted(name for name in changed_names if DUNDER.fullmatch(name))
            if special:
                found.append(Binding(owner, f"<{', '.join(special)}>", "class-call" if owner else "import-risk"))
            # A value whose kind changes (a dict made None) can break the code
            # that reads it at import, such as a metaclass building tables.
            old_kinds, new_kinds = _value_kinds(olds), _value_kinds(news)
            retyped = sorted(name for name in old_kinds.keys() & new_kinds.keys() if old_kinds[name] != new_kinds[name])
            if retyped:
                found.append(Binding(owner, f"<new kind of value {', '.join(retyped)}>", "class-call" if owner else "import-risk"))
            # An element of a new kind in an existing container (1 put in a
            # list of pairs) breaks code that unpacks it at import, such as a
            # tokenizer building its tables, and so does emptying it (QUOTES
            # made []). More elements of the kinds already there do not count.
            # A value the module or class no longer assigns (Tokenizer.QUOTES
            # deleted) fails its import-time readers, such as a metaclass
            # building tables, and import-time lines are not in the map.
            still_bound = {name for node in news for name, _ in _bound_names(node)}
            removed = sorted(old_kinds.keys() - still_bound)
            if removed:
                found.append(Binding(owner, f"<removed {', '.join(removed)}>", "class-call" if owner else "import-risk"))
            old_shapes, new_shapes = _element_shapes(olds), _element_shapes(news)
            regrown = sorted(
                name
                for name in old_shapes.keys() & new_shapes.keys()
                if new_shapes[name] - old_shapes[name] or (old_shapes[name] and not new_shapes[name])
            )
            if regrown:
                found.append(Binding(owner, f"<new element kind {', '.join(regrown)}>", "class-call" if owner else "import-risk"))
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

    global _ANNOTATIONS_RUN
    old_tree, new_tree = parse_cached(old_text), ast.parse(new_text)
    old_lazy, new_lazy = _lazy_annotations(old_tree), _lazy_annotations(new_tree)
    _ANNOTATIONS_RUN = not new_lazy
    try:
        found = compare(old_tree.body, new_tree.body, (), set())
    finally:
        _ANNOTATIONS_RUN = False
    # Dropping the future import makes every annotation in the module run.
    if old_lazy and not new_lazy:
        found.append(Binding((), "<annotations evaluated>", "import-risk"))
    return found


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
