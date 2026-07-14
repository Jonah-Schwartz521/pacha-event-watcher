"""
Read-only analysis of stock_history.jsonl.

THREE BUGS FIXED (all found once ~30h of real data existed):

1. RATES CONTAMINATED BY ROLLOVERS.
   Old code summed decreases across the whole window. When a release rolls over,
   `available` resets upward — so the OLD release draining to zero got counted as
   sales of the NEW one. franky GA showed 153/hr and "sells out in 34m". Fix:
   split each tier's history into SEGMENTS at every upward jump (rollover or
   restock) and measure only within the current segment. Same tier now reads
   0.7/hr — which is real.

2. PRICE PROJECTION IGNORED THE GAP BETWEEN RELEASES.
   BUNT GA went r3 $80 → r14 $155 → r19 $180 → r20 $185. The old code treated
   "+$75 over 11 releases" as one step, same weight as "+$5 over 1", and
   projected r21 ≈ $220. Fix: normalise to dollars PER RELEASE STEP → ~$5.5 →
   r21 ≈ $190.

3. SPREAD CRIED WOLF ON LOW-VOLUME TIERS.
   spread = fastest window ÷ slowest window. On a tier selling ~2 tickets a DAY,
   selling 2 in one hour makes 1h=2.0 and 24h=0.1 → "24x, bursty!". That's not
   burstiness, it's small-number noise: a ratio between two tiny numbers is
   meaningless. Fix: only compute spread when the tier has actually sold enough
   (MIN_VOL) for the ratio to mean something. Below that it's just "slow".

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

# A tier must have sold at least this many tickets in 24h before we'll believe a
# spread ratio or an ETA. Below it, the numbers are noise dressed up as signal.
MIN_VOL = 6

# Only above this does "bursty" mean anything.
BURSTY_AT = 3.0


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
    """Only the rows since the last rollover/restock. Both make `available` jump
    UP; measuring across that boundary counts the old release's drain as the new
    one's sales."""
    start = 0
    for i in range(1, len(rows)):
        if (rows[i]["available"] > rows[i - 1]["available"]
                or rows[i].get("name") != rows[i - 1].get("name")):
            start = i
    return rows[start:]


def sold_since(rows: list[dict], hours: float, now: datetime) -> int | None:
    """Tickets ACTUALLY LOST in the window — current release only."""
    seg = current_segment(rows)
    cutoff = now - timedelta(hours=hours)
    window = [r for r in seg if r["_t"] >= cutoff]
    if len(window) < 2:
        return None
    return sum(max(a["available"] - b["available"], 0)
               for a, b in zip(window, window[1:]))


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
    vol24 = sold_since(rows, 24, now) or 0

    # Spread only means something once enough tickets have actually moved.
    spread = None
    if vol24 >= MIN_VOL:
        rates = [r for r in (r1, r6, r24) if r is not None and r > 0]
        if len(rates) >= 2 and min(rates) > 0:
            spread = max(rates) / min(rates)

    rate = next((r for r in (r6, r24, r1) if r is not None and r > 0), None)
    eta = (latest["available"] / rate) if rate and latest["available"] > 0 else None

    seg = current_segment(rows)
    seg_h = (seg[-1]["_t"] - seg[0]["_t"]).total_seconds() / 3600 if len(seg) > 1 else 0.0

    return {
        "name": latest["name"], "price": latest["price"],
        "available": latest["available"], "quantity": latest["quantity"],
        "r1": r1, "r6": r6, "r24": r24,
        "vol24": vol24, "spread": spread, "eta_hours": eta,
        "seg_hours": seg_h,
        "trusted": vol24 >= MIN_VOL and seg_h >= 6,
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
    """Dollars PER RELEASE STEP — not per observation."""
    if len(ladder) < 2:
        return None
    per_step = []
    for (n1, p1), (n2, p2) in zip(ladder, ladder[1:]):
        gap = n2 - n1
        if gap > 0:
            per_step.append((p2 - p1) / gap)
    if not per_step:
        return None
    # Weight the most recent step highest — pricing policy can change.
    slope = (per_step[-1] * 2 + sum(per_step)) / (len(per_step) + 2)
    last_n, last_p = ladder[-1]
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
    ap.add_argument("--moving", action="store_true",
                    help="only tiers with enough volume to say anything about")
    a = ap.parse_args()

    series = load_history(a.history)
    now = datetime.now(timezone.utc)
    all_rows = [r for rows in series.values() for r in rows]
    span_h = (max(r["_t"] for r in all_rows)
              - min(r["_t"] for r in all_rows)).total_seconds() / 3600
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
                print(f"    per-release steps: " + ", ".join(f"${s:+.0f}" for s in steps))
                print(f"    → next r{n} ≈ ${p:.0f}   ({conf})")
            print()
            shown += 1
        if not shown:
            print("  No tier has rolled over yet — no ladders.\n")
        return

    print(f"{'tier':<44} {'left':>9}  {'1h':>5} {'6h':>5} {'24h':>5}  "
          f"{'24h vol':>7} {'spread':>7}  {'sells out':>9}  since roll")
    print("-" * 118)

    out = []
    for (slug, tid), rows in series.items():
        if a.slug and slug != a.slug:
            continue
        i = analyze_tier(rows, now)
        if i["available"] <= 0:
            continue
        if a.min_available and i["available"] > a.min_available:
            continue
        if a.moving and i["vol24"] < MIN_VOL:
            continue
        out.append((slug, i))

    out.sort(key=lambda x: x[1]["eta_hours"] or 9e9)

    for slug, i in out:
        if i["spread"] is None:
            spread, note = "   —   ", "  · too slow to judge" if i["vol24"] < MIN_VOL else ""
        else:
            spread = f"{i['spread']:.1f}×"
            note = "  ← BURSTY" if i["spread"] > BURSTY_AT else "  ✓ steady"
        eta = fmt_eta(i["eta_hours"]) if i["trusted"] else "—"
        print(f"{slug[:22]:<22} {i['name'][:20]:<20} {i['available']:>4}/{i['quantity']:<4} "
              f"{fmt_rate(i['r1'])} {fmt_rate(i['r6'])} {fmt_rate(i['r24'])}  "
              f"{i['vol24']:>7} {spread:>7}  {eta:>9}  {i['seg_hours']:>5.1f}h{note}")

    trusted = [i for _, i in out if i["trusted"]]
    steady = [i for i in trusted if i["spread"] and i["spread"] <= BURSTY_AT]
    print(f"""
  {len(trusted)} tier(s) have sold >={MIN_VOL} tickets in 24h AND have >=6h since their
  last rollover — those are the only ones an ETA is shown for. Of those,
  {len(steady)} are steady (spread <= {BURSTY_AT:g}x).

  Everything else is either barely selling (a ratio between two near-zero rates is
  noise, not burstiness) or just rolled over and has no track record yet.

  Rates are measured WITHIN the current release only.
""")


if __name__ == "__main__":
    main()