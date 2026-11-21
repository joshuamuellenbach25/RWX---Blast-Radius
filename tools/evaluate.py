"""Score the image the way the board does: scorer run flags, the CPU of every
process in the container added to the simulated seconds, middle of 3 runs.

The container's CPU comes from its own cgroup (cpu.stat usage_usec, user and
system together) read after the selector exits, so nothing outside the
container is counted.

Usage: python tools/evaluate.py [--data sample] [--answers sample/answers.json]
       [--runs 3] [--no-build] [--image blast-radius-entry] [--out result.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from score import evaluate_run  # noqa: E402

CPU_TRAILER = "__cpu_usage_usec__="


def run_flags(data: Path) -> list[str]:
    return [
        "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2",
        "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256", "--read-only",
        "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
        "--volume", f"{(data / 'commits').as_posix()}:/data/commits:ro",
        "--volume", f"{(data / 'tests.json').as_posix()}:/data/tests.json:ro",
    ]


def image_command(image: str) -> list[str]:
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}", image],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    entrypoint_json, cmd_json = inspected.split("|", 1)
    command = (json.loads(entrypoint_json) or []) + (json.loads(cmd_json) or [])
    if not command:
        raise SystemExit(f"image {image} has no entrypoint or command")
    return command


def run_once(image: str, data: Path, command: list[str]) -> tuple[bytes, int, float, str]:
    script = f'"$@"; status=$?; printf "\\n{CPU_TRAILER}%s\\n" "$(grep usage_usec /sys/fs/cgroup/cpu.stat)" >&2; exit $status'
    started = time.monotonic()
    completed = subprocess.run(
        ["docker", "run", *run_flags(data), "--entrypoint", "/bin/sh", image, "-c", script, "sh", *command],
        capture_output=True,
    )
    wall = time.monotonic() - started
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise SystemExit(f"selector exited {completed.returncode}; stderr tail:\n{stderr[-2000:]}")
    marker = [line for line in stderr.splitlines() if line.startswith(CPU_TRAILER)]
    if not marker:
        raise SystemExit(f"no cpu.stat line in stderr; tail:\n{stderr[-2000:]}")
    usage = marker[-1][len(CPU_TRAILER):].split()
    if len(usage) != 2 or usage[0] != "usage_usec":
        raise SystemExit(f"unreadable cpu.stat line: {marker[-1]!r}")
    return completed.stdout, int(usage[1]), wall, stderr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=REPO / "sample")
    parser.add_argument("--answers", type=Path, default=REPO / "sample" / "answers.json")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--image", default="blast-radius-entry")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--save-output", type=Path, help="write the last run's stdout here")
    options = parser.parse_args()
    data = options.data.resolve()

    if not options.no_build:
        build = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "--tag", options.image, str(REPO)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if build.returncode != 0:
            raise SystemExit(f"docker build failed:\n{build.stderr[-3000:]}")

    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads(options.answers.read_text("utf-8"))["commits"]
    command = image_command(options.image)
    runs = []
    for index in range(options.runs):
        stdout, cpu_usec, wall, stderr = run_once(options.image, data, command)
        evaluation = evaluate_run(stdout, tests, answers)
        if not evaluation["ok"]:
            raise SystemExit(f"run {index + 1} rejected: {evaluation}; stderr tail:\n{stderr[-1500:]}")
        total_usec = evaluation["simulated_milliseconds"] * 1000 + cpu_usec
        runs.append({
            "simulated_s": evaluation["simulated_milliseconds"] / 1000,
            "missed": evaluation["missed_commits"],
            "cpu_s": cpu_usec / 1e6,
            "total_s": total_usec / 1e6,
            "wall_s": round(wall, 1),
        })
        print(json.dumps(runs[-1]), flush=True)
        if options.save_output:
            options.save_output.write_bytes(stdout)
    middle = statistics.median(run["total_s"] for run in runs)
    result = {"median_total_s": round(middle, 3), "commits": len(answers), "runs": runs}
    print(f"median total {middle:.3f} s over {len(answers)} commits "
          f"({middle / len(answers):.3f} s a commit)", flush=True)
    if options.out:
        options.out.write_text(json.dumps(result, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()
