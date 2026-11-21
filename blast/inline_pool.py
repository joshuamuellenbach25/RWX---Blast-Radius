"""A stand-in for ProcessPoolExecutor that runs every call in this process.

sqlglot's optimizer and executor tests hand work to a spawned process pool.
Spawned workers import sqlglot from disk, so they would not see code changed
in memory, and their coverage is lost. This pool runs each call here instead,
and keeps the one thing the real pool does to the data: arguments, results
and errors go through a pickle round trip, as they do between processes.
"""

from __future__ import annotations

import pickle
from concurrent.futures import Future


def _round_trip(value):
    return pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))


class InlineProcessPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), *, max_tasks_per_child=None):
        # The real pool's checks on its arguments (Python 3.13 under Linux),
        # so a call that it refuses is refused here too.
        if max_workers is not None and max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        # The real pool asks a given context for its start method at once, so
        # one that is not a context (the string "spawn") fails here too.
        start_method = mp_context.get_start_method(allow_none=False) if mp_context is not None else None
        if initializer is not None and not callable(initializer):
            raise TypeError("initializer must be a callable")
        if max_tasks_per_child is not None:
            if not isinstance(max_tasks_per_child, int):
                raise TypeError("max_tasks_per_child must be an integer")
            if max_tasks_per_child <= 0:
                raise ValueError("max_tasks_per_child must be >= 1")
            if start_method == "fork":
                raise ValueError(
                    "max_tasks_per_child is incompatible with the 'fork' multiprocessing start method;"
                    " supply a different mp_context."
                )
        self._initializer = initializer
        self._initargs = initargs
        self._initialized = False

    def _initialize(self) -> None:
        if not self._initialized:
            self._initialized = True
            if self._initializer is not None:
                self._initializer(*_round_trip(self._initargs))

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future: Future = Future()
        try:
            self._initialize()
            fn, args, kwargs = _round_trip((fn, args, kwargs))
            future.set_result(_round_trip(fn(*args, **kwargs)))
        except Exception as error:  # a pool hands a call's error to whoever reads the future
            try:
                error = _round_trip(error)
            except Exception:  # an error that cannot be pickled is passed on as it is
                pass
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        return (future.result() for future in futures)

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.shutdown()
        return False


def install(modules) -> list[str]:
    """Replace ProcessPoolExecutor in every given module that imported it."""
    import concurrent.futures

    replaced = []
    concurrent.futures.ProcessPoolExecutor = InlineProcessPool
    for module in modules:
        if getattr(module, "ProcessPoolExecutor", None) is not None:
            module.ProcessPoolExecutor = InlineProcessPool
            replaced.append(module.__name__)
    return replaced
