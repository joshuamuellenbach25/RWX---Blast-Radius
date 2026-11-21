"""Check the coverage analysis against labeled commits.

For each commit: the analysis verdict per file, whether the candidate tests
hold every failing test (a miss otherwise), and the simulated cost of
running every candidate.

Usage: python tools/eval_analysis.py <source_root> <map.json> <data_dir> <answers.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from blast.analysis import CoverageMap, analyze  # noqa: E402
from blast.packing import pack  # noqa: E402


def bits(mask: int) -> list[int]:
    numbers = []
    while mask:
        low = mask & -mask
        numbers.append(low.bit_length() - 1)
        mask ^= low
    return numbers


def main() -> None:
    source_root, map_path, data_dir, answers_path = (Path(argument) for argument in sys.argv[1:5])
    coverage = CoverageMap(map_path)
    tests = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    duration = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    answers = json.loads(answers_path.read_text("utf-8"))["commits"]
    total_cost = misses = 0
    for commit, answer in sorted(answers.items()):
        patch_text = (data_dir / "commits" / f"{commit}.patch").read_text("utf-8")
        analysis = analyze(patch_text, source_root, coverage)
        candidates = set(bits(analysis.tests))
        failing = set(answer["failing"])
        missed = sorted(failing - candidates)
        cost, _ = pack(sorted(candidates), duration)
        total_cost += cost + (300_000 if missed else 0)
        misses += bool(missed)
        kinds = ",".join(f"{verdict.kind}:{verdict.path.rsplit('/', 1)[-1]}" for verdict in analysis.files)
        print(
            f"{commit} {answer['kind']:8} fail={len(failing):3} cand={len(candidates):4} "
            f"cost={cost / 1000:7.2f} {'MISS ' + str(missed) if missed else 'ok'} [{kinds}]"
        )
    print(f"total simulated {total_cost / 1000:.2f} s, {misses} misses, {len(answers)} commits")


if __name__ == "__main__":
    main()
