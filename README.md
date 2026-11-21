# Blast Radius test selector

For each patch in a batch of changes to sqlglot, a Python SQL parser and transpiler, this program
picks the tests that can fail and splits them across up to 8 workers. It is the entry of team
BackwardsDeployeds in the Blast Radius track of the Builders Buildathon.

## The event

| Item | Detail |
|---|---|
| Event | Builders Buildathon, November 20 to 22, 2026, at Ohio State |
| Organizer | Builders, which describes itself as "a student-run nonprofit" |
| Format | Kickoff and closing ceremony in person; teams may compete entirely online |
| Tracks | Four, each paying $5,000 to its winner only ($20,000 across the four) |
| Team size | One or two people |
| Judging | No judging panel: every submission is scored automatically |
| This track | Blast Radius, set by RWX, which makes a CI/CD platform that includes test suite management |
| Teams on the track | 334, as listed on the track page on November 21, 2026 |

## The problem

The track asks, for each code change, for the tests that can catch it: running fewer tests scores
better, and leaving out a failing test costs a penalty.

- **Input.** A folder of patches, each one unified diff against sqlglot at commit
  `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, and `tests.json`, which numbers every test with its
  recorded run time. The sample `tests.json` lists 1620 tests that take 103.047 s in total; three
  of them take 56.957 s.
- **Output.** One JSON line per commit, such as `{"commit": "k7q2", "workers": [[12, 845], [3]]}`.
  An empty `workers` list runs nothing.
- **Score, lower wins.** Per commit, the recorded seconds of the busiest worker plus 2 s per
  worker; 300 s more for each commit whose selection leaves out a failing test; plus the CPU
  seconds the container uses. The image runs 3 times and the middle total counts.
- **Limits.** The build gets network access, 4 cores, 8 GB and 10 minutes. A run gets no network,
  2 cores, 4 GB and 3 minutes for the whole batch (4.5 minutes in the final, which has about 1.5
  times as many commits). The repository may hold at most 20 MB.
- **Rules.** No table from a patch, or from a kind of edit, to the tests it breaks. Maps built
  from the pinned commit (coverage, imports, fixtures) are allowed.

## Approach

1. **Coverage map.** `tools/lab.Dockerfile` builds a lab image of the environment the organizers
   labeled the commits in, plus git and coverage. In it, `tools/collect_map.py` runs each test alone in a
   fork of a process that has already imported sqlglot and the test modules, and records the
   lines the test executed, the non-Python files it opened and its CPU time.
   `tools/build_map.py` packs the records into `data/map.json` (4.3 MB): the tests behind each
   line, opened file, created class and referenced name, plus what runs at import.
   `tools/build_imports.py` stores the import graph in `data/imports.json`.
2. **Bytecode comparison.** `blast/codediff.py` compiles each changed Python file before and
   after the patch. Two versions match when every code object has the same bytecode, names,
   constants and flags, with line numbers left out, so a patch that only changes comments or
   blank lines in Python files lists no tests, unless it changes an encoding declaration. A
   changed code object is either a function body, which can be swapped into a running process,
   or module-level or class-level code, which needs a new import.
3. **Static analysis.** `blast/analysis.py` turns each changed file into candidate tests: the
   tests that ran a changed function's lines; for a changed module global or class attribute,
   the tests that read the name, create the class or load the module through the import graph;
   for a changed fixture, the tests that opened it. A changed module global that code reads at
   import time lists every test when its module loads with `import sqlglot`, and so does any
   change the analysis cannot narrow down, such as a binary patch, a change to a pytest
   configuration file such as `pytest.ini`, or a file that no longer compiles.
4. **Local verification.** `blast/verify.py` forks one warm worker that imports sqlglot and the
   needed test modules once, then forks one child per commit, two at a time, one per core.
   The child points the changed functions at their new code (`blast/runner.py`) and runs the
   commit's cheap candidates with unittest, stopping at the first failure. Module-level changes
   to the sqlglot package run in a fresh interpreter that loads the patched source
   (`blast/fresh.py`), and `blast/inline_pool.py` replaces the process pool that sqlglot's
   optimizer and executor tests use with an in-process stand-in that keeps the pickle round trip.
   A clean, complete run drops the candidates from the answer.
5. **Cost gate and time budget.** `selector.py` plans a local run only when half of what a clean
   run would save covers its estimated CPU cost. Tests recorded at 7 s or more never run
   locally; such a test is left out only when a complete local run of the commit's cheaper
   candidates passes. The batch may use 60% of the run limit: analysis stops at 40% and lists
   every test for the commits it did not reach, and the worker kills any child that runs past
   its time budget.
6. **Packing.** `blast/packing.py` tries every worker count from 1 to 8, places tests longest
   first on the least-loaded worker, and keeps the count with the lowest cost.

## How correctness was guarded

- **Fail toward listing.** The selector lists every test for a commit whose patch it cannot read
  (including one over 16 MiB or not valid UTF-8) or whose analysis runs past its deadline, and
  for every commit when the coverage map is missing. A local run that crashes, times out or
  returns nothing keeps every candidate.
- **Limits of a local run.** A local run patches only its own process. A commit that changes a
  data file gets no local run, and an edited test that starts another interpreter or reads files
  itself is listed instead of run.
- **Kill tests.** Most fixes for cases where a failing test could be left out came with a kill
  test: a check that fails on the code before the fix and passes after it. The final suite had 66 checks,
  all passing on the last code on `main`. The suite lives outside this repository.
- **Sample regression check.** `tools/eval_analysis.py` checks the static analysis's candidate tests and
  `tools/evaluate.py` scores the built image against the 30 sample commits and their published
  answers. No measured version missed a failing test on them.
- **Practice board.** All 11 practice board submissions scored 0 misses.

## Results

| Measure | Value |
|---|---|
| Starter baseline: every test on 3 workers | 1614.1 s |
| Best practice score | 215.6 s |
| Misses in that submission | 0 in each of its 3 runs |

Each run of that submission recorded a simulated total of 211.297 s and 4.33 to 4.35 s of CPU.
It is commit `0c63de8` on the branch `diag-main-v7e`: `main` at `6ea5bc7` plus temporary
diagnostic code that adds at most 4.095 s of listed tests. The board never scored the final code
on `main`. As the best practice submission, `0c63de8` is the default entry for the final.

The practice board carries no prize; only the final counts. The final scores each entry on unseen
commits, including kinds of change not in practice, and scores at least the top 5 entries again
with 5 runs each. The final runs at the cutoff, 11:00 AM ET on Sunday, November 22, 2026.

## Build and run

The scorer builds the top-level `Dockerfile` for linux/amd64 and runs the image with no network,
a read-only disk and user `nobody`. The build starts from `python:3.13-slim` pinned by digest,
installs `requirements.lock`, downloads sqlglot at the pinned commit with
`tools/fetch_sqlglot.py`, compiles its bytecode, and copies in `selector.py`, `blast/` and both
maps. The image reads the organizer's data layout:

| Where | What |
|---|---|
| `/data/tests.json` | `{"tests": [...]}`, each test with `number`, `id`, `file` and `seconds` |
| `/data/commits/<id>.patch` | one unified diff against the pinned commit |
| stdout | one answer line per commit; logs go to stderr |

To build the image, run it once on the 30 sample commits under the scorer's limits and score the
output (needs Docker, make and Python 3):

    make sample

`make check` checks only the output format. `python tools/evaluate.py` builds the image, runs it 3 times with
the scorer's flags, adds each run's container CPU time from its cgroup and prints the middle total,
as the board computes it. To rebuild the maps, run `tools/collect_map.py`, `tools/build_map.py`
and `tools/build_imports.py` inside the image `tools/lab.Dockerfile` builds; `tools/label.py`
labels patches by running the whole suite on each. Each script's docstring gives its usage.

## Repository layout

    Dockerfile             image the scorer builds and runs
    selector.py            entry point: analyze, verify, pack, print the answers
    blast/analysis.py      patch to candidate tests, using the coverage map
    blast/codediff.py      bytecode and statement comparison of two file versions
    blast/patch.py         unified diff reader and applier
    blast/verify.py        warm worker and per-commit child processes
    blast/runner.py        function code swaps, patched-source loader, unittest runner
    blast/fresh.py         fresh-interpreter runs for module-level changes
    blast/inline_pool.py   in-process stand-in for ProcessPoolExecutor
    blast/packing.py       splits the chosen tests across workers
    data/map.json          per-test coverage map of the pinned commit
    data/imports.json      import graph of the pinned commit
    tools/                 sqlglot download, map building, labeling, local scoring, standings reader
    docs/STATUS.md         status log kept during the event
    Makefile, score.py, SCORING.md, requirements.lock, sample/
                           the organizer's starter kit files, unchanged

## Sources

Event and track facts come from these pages, read on November 21, 2026: the track page
(https://www.joinbuilders.org/buildathon/tracks/blast-radius), the event page
(https://www.joinbuilders.org/buildathon), the Builders home page (https://www.joinbuilders.org/)
and the RWX home page (https://www.rwx.com/). Test counts and seconds come from `sample/tests.json`,
the scoring arithmetic from `SCORING.md`, and the practice score from the track's practice board.
