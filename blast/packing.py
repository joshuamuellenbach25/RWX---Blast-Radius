"""Split chosen tests across workers for the lowest simulated cost."""

from __future__ import annotations

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def pack(numbers: list[int], duration_ms: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Return (cost in ms, workers). Cost is the busiest worker's load plus
    the worker cost for every worker; no tests costs nothing."""
    if not numbers:
        return 0, []
    ordered = sorted(numbers, key=lambda number: (-duration_ms[number], number))
    best_cost, best_workers = None, None
    for worker_count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        loads = [0] * worker_count
        workers: list[list[int]] = [[] for _ in range(worker_count)]
        for number in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += duration_ms[number]
        cost = max(loads) + WORKER_COST_MS * worker_count
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_cost, best_workers
