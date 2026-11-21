"""Run a commit's cheap candidate tests in a fresh interpreter whose changed
modules load from the patched text.

A change to code that runs when a module is imported (a class table, a
module constant) cannot be applied by pointing functions at new code: every
module that copied the old value at import keeps it. Here the patched text is
served before the first sqlglot import, so the whole package is built from
it, as it is when the commit's tests run. The verify worker's child execs
this module; the result file has the same shape as a swap job's, plus
"import_failed" when a changed package module does not import at all.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path


def _write(path: str, result: dict) -> None:
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result), "utf-8")
    os.replace(temporary, target)


def main(spec_path: str) -> None:
    """Never returns: the result goes to the spec's result file."""
    code = 0
    try:
        spec = json.loads(Path(spec_path).read_text("utf-8"))
        # The spec holds whole patched files and /tmp has 64 MB: gone once read.
        Path(spec_path).unlink(missing_ok=True)
        root = spec["root"]
        os.chdir(root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from blast import runner

        runner.quiet_stdout()
        texts = {os.path.join(root, path): text for _, path, text in spec["sources"]}
        sys.meta_path.insert(0, runner.PatchedSourceFinder(texts))
        result = {"commit": spec["commit"], "ok": True, "failed": [], "summary": {}, "fresh": True}
        for name, path, _ in spec["sources"]:
            if not path.startswith("sqlglot/"):
                continue  # a test module that fails to load fails its own tests below
            try:
                importlib.import_module(name)
            except BaseException as error:  # SystemExit too: every importer of the module fails
                result["import_failed"] = name
                print(f"{spec['commit']}: patched {name} fails to import: {error!r}", file=sys.stderr, flush=True)
                _write(spec["result"], result)
                return
        limits = {int(number): limit for number, limit in spec["limits"].items()}
        failed, summary = runner.run_tests(spec["tests"], limits, stop_at_first_failure=True)
        result.update(failed=sorted(failed), summary=summary)
        _write(spec["result"], result)
    except BaseException as error:  # the worker reads a missing result as no answer
        print(f"fresh run failed: {error!r}", file=sys.stderr, flush=True)
        code = 1
    finally:
        os._exit(code)
