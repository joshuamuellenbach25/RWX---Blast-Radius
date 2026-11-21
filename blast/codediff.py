"""Compare the old and new versions of one Python file.

Two compiled versions behave the same when every code object has the same
bytecode, names, constants and flags; line numbers are left out, so a change
that only adds comments or blank lines compares equal. Each changed code
object is either inside a function body (its __code__ can be swapped in a
live process) or at module or class level (the module has to be re-imported).
"""

from __future__ import annotations

import ast
import dis
import inspect
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
        return self.is_function and self.old.co_freevars == self.new.co_freevars


@dataclass
class FileDiff:
    syntax_error: bool = False
    changed: list[ChangedCode] = field(default_factory=list)
    unmatched: list[tuple] = field(default_factory=list)  # code objects added or removed

    @property
    def same_behavior(self) -> bool:
        return not self.syntax_error and not self.changed and not self.unmatched

    @property
    def function_level_only(self) -> bool:
        return (
            not self.syntax_error
            and not self.unmatched
            and all(change.swappable for change in self.changed)
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


def code_tree(code: CodeType) -> dict[tuple, CodeType]:
    """Every code object under `code`, keyed by its qualname and its position
    among siblings with the same qualname."""
    tree: dict[tuple, CodeType] = {}

    def visit(node: CodeType) -> None:
        base = node.co_qualname
        index = 0
        while (base, index) in tree:
            index += 1
        tree[(base, index)] = node
        for const in node.co_consts:
            if isinstance(const, CodeType):
                visit(const)

    visit(code)
    return tree


def compile_text(text: str, filename: str) -> CodeType:
    return compile(text, filename, "exec", dont_inherit=True, optimize=0)


def diff_python(old_text: str, new_text: str, filename: str) -> FileDiff:
    old_code = compile_text(old_text, filename)
    try:
        new_code = compile_text(new_text, filename)
    except (SyntaxError, ValueError):
        return FileDiff(syntax_error=True)
    old_tree = code_tree(old_code)
    new_tree = code_tree(new_code)
    result = FileDiff()
    for key in old_tree.keys() | new_tree.keys():
        if key not in old_tree or key not in new_tree:
            result.unmatched.append(key)
            continue
        old_node, new_node = old_tree[key], new_tree[key]
        if fingerprint(old_node) != fingerprint(new_node):
            result.changed.append(ChangedCode(key, old_node, new_node))
    return result


def statement_spans(source: str) -> list[tuple[int, int]]:
    """(first, last) line of every statement; for a compound statement only
    its header, since its body statements are listed on their own."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
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
