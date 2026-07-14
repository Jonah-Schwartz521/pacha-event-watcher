"""
Read-only analysis of stock_history.jsonl.

TWO BUGS FIXED (both found once 29h of real data existed):

1. RATES WERE CONTAMINATED BY ROLLOVERS.
   Old code summed every decrease across the whole window. When a release rolls
   over, `available` resets upward — and the old release draining to zero got
   counted as sales of the NEW release. That's why franky GA showed 153/hr with a
   76x spread. Fix: split each tier's history into SEGMENTS at every upward jump
   (rollover or restock) and only measure within the current segment.

2. PRICE PROJECTION IGNORED THE GAP BETWEEN RELEASES.
   Old code averaged raw price deltas between observations. BUNT GA went
   r3 $80 → r14 $155 → r19 $180 → r20 $185, and it treated "+$75 over 11
   releases" the same as "+$5 over 1", projecting r21 ≈ $220. Fix: normalise to
   dollars PER RELEASE STEP. Same data gives ~$5/release → r21 ≈ $190.

    python -m src.analyze
    python -m src.analyze --ladder
    python -m src.analyze --slug bunt-24-07-2026
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HISTORY = os.getenv("HISTORY_PATH", "/app/data/stock_history.jsonl")
RELEASE_RE = re.compile(r"^(?P<base>.*?)\s*[-–]\s*(?P<n>\d+)(?:st|nd|rd|th)\s+Release\s*$", re.I)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_history(path: str) -> dict[tuple[str, str], list[dict]]:
    series: dict[tuple[str, str], list[dict]] = defaultdict(list)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r["_t"] = parse_ts(r["ts"])
                series[(r["slug"], r["tier_id"])].append(r)
    except FileNotFoundError:
        raise SystemExit(f"no history at {path} — has the watcher run yet?")

    for rows in series.values():
        rows.sort(key=lambda r: r["_t"])
    return series


def current_segment(rows: list[dict]) -> list[dict]:
    """Only the rows since the last rollover/restock.

    A release rollover (or a restock) makes `available` jump UP. Measuring sales
    across that boundary counts the old release's drain as the new one's sales.
    So: walk backwards to the last upward jump and measure only after it.
    """
    start = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if cur["available"] > prev["available"] or cur.get("name") != prev.get("name"):
            start = i
    return rows[start:]


def sold_since(rows: list[dict], hours: float, now: datetime) -> int | None:
    """Tickets ACTUALLY LOST in the window — within the current release only."""
    seg = current_segment(rows)
    cutoff = now - timedelta(hours=hours)
    window = [r for r in seg if r["_t"] >= cutoff]
    if len(window) < 2:
        return None
    sold = 0
    for a, b in zip(window, window[1:]):
        d = a["available"] - b["available"]
        if d > 0:
            sold += d
    return sold


def rate_over(rows: list[dict], hours: float, now: datetime) -> float | None:
    seg = current_segment(rows)
    cutoff = now - timedelta(hours=hours)
    window = [r for r in seg if r["_t"] >= cutoff]
    if len(window) < 2:
        return None
    sold = sum(max(a["available"] - b["available"], 0)
               for a, b in zip(window, window[1:]))
    span = (window[-1]["_t"] - window[0]["_t"]).total_seconds() / 3600
    return sold / span if span > 0 else None


def analyze_tier(rows: list[dict], now: datetime) -> dict:
    latest = rows[-1]
    r1, r6, r24 = (rate_over(rows, h, now) for h in (1, 6, 24))
    rates = [r for r in (r1, r6, r24) if r is not None and r > 0]
    spread = (max(rates) / min(rates)) if len(rates) >= 2 and min(rates) > 0 else None
    rate = next((r for r in (r6, r24, r1) if r is not None and r > 0), None)
    eta = (latest["available"] / rate) if rate and latest["available"] > 0 else None
    seg = current_segment(rows)
    return {
        "name": latest["name"], "price": latest["price"],
        "available": latest["available"], "quantity": latest["quantity"],
        "r1": r1, "r6": r6, "r24": r24, "spread": spread, "eta_hours": eta,
        "seg_hours": (seg[-1]["_t"] - seg[0]["_t"]).total_seconds() / 3600 if len(seg) > 1 else 0,
    }


def fmt_rate(r: float | None) -> str:
    return "  —  " if r is None else f"{r:5.1f}"


def fmt_eta(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{h*60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def price_ladders(series):
    ladders: dict[tuple[str, str], set] = defaultdict(set)
    for key, rows in series.items():
        for r in rows:
            m = RELEASE_RE.match(r.get("name") or "")
            if m:
                ladders[key].add((int(m.group("n")), float(r["price"])))
    return {k: sorted(v) for k, v in ladders.items() if v}


def project_next(ladder: list[tuple[int, float]]):
    """Dollars PER RELEASE STEP — not per observation.

    BUNT GA: r3 $80 -> r14 $155 -> r19 $180 -> r20 $185
      r3->r14 : +75 / 11 steps = $6.8
      r14->r19: +25 /  5 steps = $5.0
      r19->r20: + 5 /  1 step  = $5.0
    => ~$5.5/release => r21 ~ $190.  (The old code said $220.)
    """
    if len(ladder) < 2:
        return None
    per_step, steps_seen = [], 0
    for (n1, p1), (n2, p2) in zip(ladder, ladder[1:]):
        gap = n2 - n1
        if gap <= 0:
            continue
        per_step.append((p2 - p1) / gap)
        steps_seen += gap
    if not per_step:
        return None
    # Weight the most recent step highest — pricing policy can change.
    slope = (per_step[-1] * 2 + sum(per_step)) / (len(per_step) + 2)
    last_n, last_p = ladder[-1]
    # Confidence comes from CONSISTENCY, not from how many points we happen to have.
    if len(per_step) >= 2:
        lo, hi = min(per_step), max(per_step)
        conf = "solid" if hi <= lo * 1.6 + 1 else "irregular"
    else:
        conf = "weak — one step observed"
    return last_n + 1, last_p + slope, conf, per_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=HISTORY)
    ap.add_argument("--slug")
    ap.add_argument("--ladder", action="store_true")
    ap.add_argument("--min-available", type=int, default=0)
    a = ap.parse_args()

    series = load_history(a.history)
    now = datetime.now(timezone.utc)
    all_rows = [r for rows in series.values() for r in rows]
    span_h = (max(r["_t"] for r in all_rows) - min(r["_t"] for r in all_rows)).total_seconds() / 3600

    print(f"history: {len(all_rows):,} rows · {len(series)} tiers · {span_h:.1f}h\n")

    if a.ladder:
        print("OBSERVED PRICE LADDERS (only releases the watcher has witnessed)\n")
        shown = 0
        for (slug, tid), lad in sorted(price_ladders(series).items()):
            if a.slug and slug != a.slug:
                continue
            if len(lad) < 2:
                continue
            rows = series[(slug, tid)]
            m = RELEASE_RE.match(rows[-1]["name"] or "")
            base = m.group("base") if m else rows[-1]["name"]
            print(f"  {slug}")
            print(f"    {base}: " + " → ".join(f"r{n} ${p:g}" for n, p in lad))
            proj = project_next(lad)
            if proj:
                n, p, conf, steps = proj
                seq = ", ".join(f"${s:+.0f}" for s in steps)
                print(f"    per-release steps: {seq}")
                print(f"    → next r{n} ≈ ${p:.0f}   ({conf})")
            print()
            shown += 1
        if not shown:
            print("  No tier has rolled over yet — no ladders.\n")
        return

    print(f"{'tier':<44} {'left':>9}  {'1h':>5} {'6h':>5} {'24h':>5}  "
          f"{'spread':>6}  {'sells out':>9}  since roll")
    print("-" * 112)

    out = []
    for (slug, tid), rows in series.items():
        if a.slug and slug != a.slug:
            continue
        i = analyze_tier(rows, now)
        if i["available"] <= 0:
            continue
        if a.min_available and i["available"] > a.min_available:
            continue
        out.append((slug, i))

    out.sort(key=lambda x: x[1]["eta_hours"] or 9e9)

    for slug, i in out:
        spread = f"{i['spread']:.1f}×" if i["spread"] else "  —  "
        warn = "  ← bursty" if i["spread"] and i["spread"] > 3 else ""
        print(f"{slug[:22]:<22} {i['name'][:20]:<20} {i['available']:>4}/{i['quantity']:<4} "
              f"{fmt_rate(i['r1'])} {fmt_rate(i['r6'])} {fmt_rate(i['r24'])}  "
              f"{spread:>6}  {fmt_eta(i['eta_hours']):>9}  {i['seg_hours']:>5.1f}h{warn}")

    print("""
  Rates are now measured WITHIN the current release only — a rollover or restock
  starts a fresh segment, so the old release draining to zero no longer shows up
  as sales of the new one. "since roll" = hours of data in the current segment;
  if that's small, the rate is based on very little and the ETA means little.
""")


if __name__ == "__main__":
    main()