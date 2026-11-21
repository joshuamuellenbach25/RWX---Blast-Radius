"""Choose the tests each patch can break and split them across workers.

For every /data/commits/<id>.patch the coverage map built from the pinned
sqlglot commit names the tests that run the changed code, read the changed
fixture or depend on the changed class attribute. A patch that changes no
behavior (comments, blank lines) runs nothing. Anything the analysis cannot
read runs every test. Answers go to stdout, one JSON line per commit; logs
go to stderr.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

from blast.analysis import CoverageMap, analyze  # noqa: E402
from blast.packing import pack  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SOURCE_ROOT = Path(os.environ.get("BLAST_SOURCE", "/sqlglot"))
MAP_PATH = APP / "data" / "map.json"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def bits(mask: int) -> list[int]:
    numbers = []
    while mask:
        low = mask & -mask
        numbers.append(low.bit_length() - 1)
        mask ^= low
    return numbers


def main() -> None:
    started = time.monotonic()
    answer_fd = os.dup(1)
    # Anything printed by accident lands on stderr, never among the answers.
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    all_numbers = sorted(test["number"] for test in tests)
    duration_ms = {test["number"]: math.floor(test["seconds"] * 1000 + 0.5) for test in tests}
    commits = sorted(path.stem for path in (DATA / "commits").glob("*.patch"))

    coverage = None
    try:
        coverage = CoverageMap(MAP_PATH)
    except Exception as error:  # without the map every commit runs every test
        log(f"map unreadable, running every test: {error!r}")
    translate: list[int | None] = []
    unknown_tests: list[int] = []
    if coverage is not None:
        number_by_id = {test["id"]: test["number"] for test in tests}
        translate = [number_by_id.get(test_id) for test_id in coverage.test_ids]
        known = set(coverage.test_ids)
        unknown_tests = [test["number"] for test in tests if test["id"] not in known]
        if unknown_tests:
            log(f"{len(unknown_tests)} tests are not in the map; they run with every changed commit")

    lines = []
    for commit in commits:
        chosen = all_numbers
        note = "fallback"
        if coverage is not None:
            try:
                text = (DATA / "commits" / f"{commit}.patch").read_text("utf-8", errors="replace")
                analysis = analyze(text, SOURCE_ROOT, coverage)
                mask = analysis.tests
                chosen = sorted({translate[n] for n in bits(mask) if translate[n] is not None})
                if mask and unknown_tests:
                    chosen = sorted(set(chosen) | set(unknown_tests))
                note = ",".join(f"{verdict.kind}:{verdict.path}" for verdict in analysis.files)
            except Exception as error:  # an unreadable commit runs every test
                log(f"{commit}: analysis failed, running every test: {error!r}")
                chosen = all_numbers
        cost, workers = pack(chosen, duration_ms)
        lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
        log(f"{commit}: {len(chosen)} tests, {cost / 1000:.2f} s [{note}]")

    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    os.write(answer_fd, payload)
    log(f"{len(commits)} commits answered in {time.monotonic() - started:.2f} s")


if __name__ == "__main__":
    main()
