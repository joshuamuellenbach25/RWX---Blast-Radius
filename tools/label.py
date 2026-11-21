"""Label patches by running the whole sqlglot suite on each one.

Runs inside the lab image. For every <id>.patch in the patch folder it copies
the pinned checkout, applies the patch with git, runs pytest and writes one
JSON line: {"commit": id, "failing": [test numbers], "seconds": wall time}.
Finished ids in the output file are skipped, so a stopped run resumes.

Usage: python label.py <patch_dir> <tests.json> <out.jsonl> [--jobs 4]
       [--timeout 120] [--base]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

CHECKOUT = Path("/sqlglot")
WORK = Path("/work")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR|SUBFAILED\S*)\s+(tests/\S+)")
BASE_ID = "__base__"


def run_captured(command: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_failing_ids(pytest_output: str) -> tuple[set[str], set[str]]:
    """Return (failing test ids, test files that could not be collected)."""
    failing_ids: set[str] = set()
    broken_files: set[str] = set()
    for line in pytest_output.splitlines():
        match = SUMMARY_LINE.match(line)
        if not match:
            continue
        node = match.group(2)
        if "::" in node:
            failing_ids.add(node)
        else:
            broken_files.add(node)
    return failing_ids, broken_files


def label_one(commit: str, patch_path: Path | None, tests: list[dict], timeout: int) -> dict:
    workdir = WORK / commit
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(CHECKOUT, workdir)
    started = time.monotonic()
    try:
        if patch_path is not None:
            applied = run_captured(
                ["git", "apply", "--whitespace=nowarn", str(patch_path)], workdir
            )
            if applied.returncode != 0:
                return {"commit": commit, "error": "patch_failed", "detail": applied.stderr[-500:]}
        environment = dict(os.environ, PYTHONPATH=str(workdir), SKIP_INTEGRATION="0")
        run = run_captured(
            [
                sys.executable, "-m", "pytest", "tests", "-q", "-rA",
                "-p", "no:cacheprovider", f"--timeout={timeout}",
                f"--junitxml={WORK / (commit + '.xml')}",
            ],
            workdir,
            environment,
        )
        output = run.stdout
        # pytest exits 0 when all pass and 1 when tests fail. Anything else
        # (a kill for memory, an internal error) is no label at all.
        if run.returncode not in (0, 1):
            return {
                "commit": commit,
                "error": f"pytest_exit_{run.returncode}",
                "detail": output[-300:],
            }
        failing_ids, broken_files = read_failing_ids(output)
        known_ids = {test["id"] for test in tests}
        failing_numbers = sorted(
            test["number"]
            for test in tests
            if test["id"] in failing_ids or test["file"] in broken_files
        )
        tail = output.strip().splitlines()[-1] if output.strip() else ""
        return {
            "commit": commit,
            "failing": failing_numbers,
            "unknown_ids": sorted(failing_ids - known_ids),
            "broken_files": sorted(broken_files),
            "returncode": run.returncode,
            "tail": tail,
            "seconds": round(time.monotonic() - started, 1),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch_dir", type=Path)
    parser.add_argument("tests_json", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--base", action="store_true", help="also run the unpatched checkout")
    options = parser.parse_args()

    tests = json.loads(options.tests_json.read_text("utf-8"))["tests"]
    done: set[str] = set()
    if options.out.exists():
        for line in options.out.read_text("utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if "error" not in record:
                    done.add(record["commit"])

    jobs: list[tuple[str, Path | None]] = []
    if options.base and BASE_ID not in done:
        jobs.append((BASE_ID, None))
    for patch_path in sorted(options.patch_dir.glob("*.patch")):
        if patch_path.stem not in done:
            jobs.append((patch_path.stem, patch_path))
    print(f"{len(jobs)} to label, {len(done)} already done", file=sys.stderr, flush=True)

    WORK.mkdir(parents=True, exist_ok=True)
    write_lock = Lock()

    def run(job: tuple[str, Path | None]) -> None:
        result = label_one(job[0], job[1], tests, options.timeout)
        with write_lock:
            with options.out.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(result) + "\n")
            print(f"{job[0]} {result.get('tail', result.get('error'))}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main()
