"""
Persistent state for the Pacha watcher.

Schema (v1):
{
  "version": 1,
  "events": {
    "<slug>": {
      "name": "BUNT.",
      "date": "2026-07-24T22:00:00-04:00",
      "first_seen": "2026-07-11T19:33:59Z",
      "tiers": {
        "<tier_id>": {                 # tickets[]._id — the PRODUCT
          "release_id": "1775242144942",   # current_price._id — changes on rollover
          "name": "General Access - 17th Release",
          "release_no": 17,
          "price": 170,
          "quantity": 20,
          "available": 4,
          "severity": "critical"       # latch: none < low < critical < sold_out
        }
      }
    }
  }
}

The `severity` latch is what stops the channel flooding: a tier pings at most
once per severity level, and the ladder only ever ratchets downward — until a
release rollover resets it (new release = new allocation = watch it again).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# ordered ladder — index is the comparison
SEVERITIES = ("none", "low", "critical", "sold_out")


def severity_rank(s: str) -> int:
    try:
        return SEVERITIES.index(s)
    except ValueError:
        return 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"version": SCHEMA_VERSION, "events": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Corrupt state is worse than no state, but silently resetting would
        # re-alert everything. Refuse to run instead.
        raise RuntimeError(
            f"State file at {path} is unreadable. Inspect or delete it "
            f"(deleting means the next run re-seeds silently)."
        )

    if not isinstance(data, dict) or "events" not in data:
        # This is what a Concourse state file looks like: a bare list of IDs.
        raise RuntimeError(
            f"State file at {path} isn't Pacha state (wrong schema). "
            f"If this is left over from another scraper, delete it."
        )
    return data


def save(path: str, state: dict) -> None:
    """Atomic write — a crash mid-write must not corrupt state."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = SCHEMA_VERSION
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)          # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_history(path: str, events) -> None:
    """One JSONL line per tier per cycle. Free velocity data — the counts are
    already in the response, and you can't reconstruct this retroactively."""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    with p.open("a", encoding="utf-8") as f:
        for e in events:
            for t in e.tiers:
                f.write(json.dumps({
                    "ts": ts,
                    "slug": e.slug,
                    "tier_id": t.tier_id,
                    "name": t.name,
                    "price": t.price,
                    "quantity": t.quantity,
                    "available": t.available,
                }, ensure_ascii=False) + "\n")