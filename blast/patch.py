"""Read a unified diff and apply it to the pinned checkout's files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    """The patch cannot be read or does not apply to the pinned files."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)  # each starts with " ", "-" or "+"


@dataclass
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    mode_change: bool = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""

    @property
    def paths(self) -> set[str]:
        return {path for path in (self.old_path, self.new_path) if path}

    def changed_old_lines(self) -> set[int]:
        """Old line numbers that were removed or replaced, plus the old lines
        on both sides of every pure insertion (a run of added lines with no
        removed line: a replacement's added lines run where its removed ones
        ran, so its neighbors add nothing)."""
        changed: set[int] = set()
        for hunk in self.hunks:
            old_line = hunk.old_start
            removed, inserted_at = False, None
            for line in [*hunk.lines, " "]:  # the sentinel closes the last run
                tag = line[:1]
                if tag == "-":
                    changed.add(old_line)
                    removed = True
                    old_line += 1
                elif tag == "+":
                    if inserted_at is None:
                        inserted_at = old_line
                elif tag == " ":
                    if inserted_at is not None and not removed:
                        changed.update((inserted_at - 1, inserted_at))
                    removed, inserted_at = False, None
                    old_line += 1
        return {line for line in changed if line > 0}


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
            if not match:
                raise PatchError(f"unreadable diff header: {line!r}")
            current = FilePatch(old_path=match.group(1), new_path=match.group(2))
            patches.append(current)
            index += 1
            continue
        # A plain unified diff starts each file with a ---/+++ pair and no
        # git header: after a file's hunks such a pair opens the next file,
        # where it would otherwise rename the file just read.
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
            and (current is None or current.hunks)
        ):
            current = FilePatch(old_path=_strip_prefix(line[4:]), new_path=_strip_prefix(lines[index + 1][4:]))
            patches.append(current)
            index += 2
            continue
        if current is None:
            index += 1
            continue
        if line.startswith("new file mode"):
            current.old_path = None
        elif line.startswith("deleted file mode"):
            current.new_path = None
        elif line.startswith(("old mode", "new mode")):
            current.mode_change = True
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to "):]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("--- "):
            current.old_path = _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            current.new_path = _strip_prefix(line[4:])
        elif line.startswith("@@"):
            match = HUNK_HEADER.match(line)
            if not match:
                raise PatchError(f"unreadable hunk header: {line!r}")
            hunk = Hunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2)) if match.group(2) is not None else 1,
                new_start=int(match.group(3)),
                new_count=int(match.group(4)) if match.group(4) is not None else 1,
            )
            current.hunks.append(hunk)
            index += 1
            old_seen = new_seen = 0
            while index < len(lines) and (old_seen < hunk.old_count or new_seen < hunk.new_count):
                body = lines[index]
                if body.startswith("\\"):
                    index += 1
                    continue
                tag = body[:1] if body else " "
                if tag not in " -+":
                    raise PatchError(f"unexpected hunk line: {body!r}")
                hunk.lines.append(body if body else " ")
                if tag in " -":
                    old_seen += 1
                if tag in " +":
                    new_seen += 1
                index += 1
            continue
        index += 1
    return patches


def apply_file_patch(old_text: str | None, patch: FilePatch) -> str | None:
    """Return the new text of one file, or None when the patch deletes it."""
    if patch.new_path is None:
        return None
    if patch.binary:
        raise PatchError(f"binary patch for {patch.path}")
    old_lines = [] if old_text is None else old_text.split("\n")
    trailing_newline = old_text is None or old_text.endswith("\n")
    if old_text is not None and trailing_newline:
        old_lines.pop()
    new_lines: list[str] = []
    cursor = 0  # index into old_lines
    for hunk in patch.hunks:
        start = hunk.old_start - 1 if hunk.old_count > 0 else hunk.old_start
        if start < cursor or start > len(old_lines):
            raise PatchError(f"hunk out of order in {patch.path}")
        new_lines.extend(old_lines[cursor:start])
        cursor = start
        for line in hunk.lines:
            tag, body = line[:1], line[1:]
            if tag == "+":
                new_lines.append(body)
                continue
            if cursor >= len(old_lines) or old_lines[cursor] != body:
                raise PatchError(f"context mismatch in {patch.path} at line {cursor + 1}")
            if tag == " ":
                new_lines.append(body)
            cursor += 1
    new_lines.extend(old_lines[cursor:])
    text = "\n".join(new_lines)
    if new_lines and trailing_newline:
        text += "\n"
    return text
