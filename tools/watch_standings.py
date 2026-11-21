"""Keep a live target from the track's public standings.

Reads the public JSON board (no sign-in), and writes the leader, the target
(leader minus a margin, since lower wins) and our own row when --team is
given. A failed read keeps the last good values and marks the state stale,
so a reader can tell fresh data from old data by `status` and `checked_at`.

Usage: python watch_standings.py --output live-target.json [--team NAME]
       [--interval 60] [--once] [--margin 1.0]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

URL = "https://www.joinbuilders.org/buildathon/standings/blast-radius"


def summarize(board: dict, team: str | None, margin: float) -> dict:
    if board.get("key") != "blast-radius" or board.get("direction") != "lower":
        raise ValueError(f"unexpected board key={board.get('key')!r} direction={board.get('direction')!r}")
    rows = board["standings"]
    for row in rows:
        score = row.get("score")
        if not isinstance(row.get("team"), str) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError(f"malformed standing row: {row!r}")
    others = [row for row in rows if row["team"] != team]
    leader = min(others, key=lambda row: (row["score"], row["at"])) if others else None
    own = next((row for row in rows if row["team"] == team), None) if team else None
    # The target moves with the board: the best other team's score, less a
    # lead of `margin` (a fraction of that score when below 1).
    lead = leader["score"] * margin if leader and margin < 1 else margin
    target = round(leader["score"] - lead, 3) if leader else None
    return {
        "leader_team": leader["team"] if leader else None,
        "leader_score": leader["score"] if leader else None,
        "margin": margin,
        "target_score": target,
        "team": team,
        "our_score": own["score"] if own else None,
        "our_rank": own["rank"] if own else None,
        "gap_to_target": round(own["score"] - target, 3) if own and target is not None else None,
        "top5": [[row["rank"], row["score"], row["team"]] for row in sorted(rows, key=lambda r: r["rank"])[:5]],
        "team_count": board.get("team_count", len(rows)),
        "cutoff_at": board.get("cutoff_at"),
        "closed": bool(board.get("closed")),
        "scorer_version": board.get("scorer_version"),
    }


def poll(previous: dict, team: str | None, margin: float) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        request = Request(URL, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
        with urlopen(request, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("standings response larger than 2 MB")
        state = summarize(json.loads(raw), team, margin)
        return dict(state, status="fresh", checked_at=now, last_success_at=now)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(previous, status="stale", checked_at=now, error=f"{type(error).__name__}: {error}")


def write_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--team")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--margin", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    if options.interval < 15:
        parser.error(f"interval must be at least 15 seconds, got {options.interval}")
    previous: dict = {}
    if options.output.exists():
        try:
            previous = json.loads(options.output.read_text("utf-8"))
        except ValueError:
            previous = {}
    while True:
        state = poll(previous, options.team, options.margin)
        if state.get("leader_score") != previous.get("leader_score") or state.get("our_score") != previous.get("our_score"):
            history = options.output.with_suffix(".history.jsonl")
            with history.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(state) + "\n")
        write_state(state, options.output)
        previous = state
        if options.once or state.get("closed"):
            break
        time.sleep(options.interval)


if __name__ == "__main__":
    main()
