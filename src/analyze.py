"""
Read-only analysis of stock_history.jsonl. Computes nothing the watcher needs —
this exists so a human can judge whether the data is trustworthy ENOUGH to
forecast from before we put a forecast in front of someone making money decisions.

    docker compose exec watcher python -m src.analyze
    docker compose exec watcher python -m src.analyze --slug bunt-24-07-2026
    docker compose exec watcher python -m src.analyze --ladder

The central question it answers: ARE BURN RATES STABLE?

We compute the rate over three windows (1h / 6h / 24h). If they broadly agree,
a linear ETA is meaningful. If they wildly disagree, sales are bursty and any
single "sells out in ~2h" is a confident lie — which, in a resale context, is
worse than saying nothing. Look at the SPREAD column before trusting the ETA.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HISTORY = os.getenv("HISTORY_PATH", "/app/data/stock_history.jsonl")
STATE = os.getenv("STATE_PATH", "/app/data/pacha_state.json")

RELEASE_RE = re.compile(r"^(?P<base>.*?)\s*[-–]\s*(?P<n>\d+)(?:st|nd|rd|th)\s+Release\s*$", re.I)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_history(path: str) -> dict[tuple[str, str], list[dict]]:
    """-> {(slug, tier_id): [rows sorted by time]}"""
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


def rate_over(rows: list[dict], hours: float, now: datetime) -> float | None:
    """Tickets sold per hour over the trailing window. None if not enough data.

    Only counts DECREASES. A release rollover resets `available` upward, and
    counting that as negative sales would produce nonsense.
    """
    cutoff = now - timedelta(hours=hours)
    window = [r for r in rows if r["_t"] >= cutoff]
    if len(window) < 2:
        return None

    sold = 0
    for a, b in zip(window, window[1:]):
        delta = a["available"] - b["available"]
        if delta > 0:                      # ignore rollovers / restocks
            sold += delta

    span = (window[-1]["_t"] - window[0]["_t"]).total_seconds() / 3600
    if span <= 0:
        return None
    return sold / span


def analyze_tier(rows: list[dict], now: datetime) -> dict:
    latest = rows[-1]
    r1 = rate_over(rows, 1, now)
    r6 = rate_over(rows, 6, now)
    r24 = rate_over(rows, 24, now)

    rates = [r for r in (r1, r6, r24) if r is not None and r > 0]
    # Spread = how much the windows disagree. High spread => bursty => ETA is a lie.
    spread = (max(rates) / min(rates)) if len(rates) >= 2 and min(rates) > 0 else None

    # Prefer the 6h rate as the working estimate; fall back outward.
    rate = next((r for r in (r6, r24, r1) if r is not None and r > 0), None)
    eta = (latest["available"] / rate) if rate and latest["available"] > 0 else None

    return {
        "name": latest["name"], "price": latest["price"],
        "available": latest["available"], "quantity": latest["quantity"],
        "r1": r1, "r6": r6, "r24": r24, "spread": spread, "eta_hours": eta,
        "samples": len(rows),
        "span_hours": (rows[-1]["_t"] - rows[0]["_t"]).total_seconds() / 3600,
    }


def fmt_rate(r: float | None) -> str:
    if r is None:
        return "  —  "
    return f"{r:5.1f}"


def fmt_eta(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{h*60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def price_ladders(series) -> dict[tuple[str, str], list[tuple[int, float]]]:
    """Reconstruct each tier's observed release ladder: [(release_no, price)].

    IMPORTANT: only includes releases the watcher actually WITNESSED. It started
    logging today, so early ladders will be 1-2 entries — not enough to project
    from. This grows more useful every day.
    """
    ladders: dict[tuple[str, str], set] = defaultdict(set)
    for key, rows in series.items():
        for r in rows:
            m = RELEASE_RE.match(r.get("name") or "")
            if m:
                ladders[key].add((int(m.group("n")), float(r["price"])))
    return {k: sorted(v) for k, v in ladders.items() if v}


def project_next(ladder: list[tuple[int, float]]) -> tuple[int, float, str] | None:
    """Guess the next release number + price from the observed ladder."""
    if len(ladder) < 2:
        return None
    steps = [b[1] - a[1] for a, b in zip(ladder, ladder[1:])
             if b[0] > a[0] and b[1] != a[1]]
    if not steps:
        return None
    avg = sum(steps) / len(steps)
    last_n, last_p = ladder[-1]
    confidence = "solid" if len(steps) >= 3 else "weak"
    return last_n + 1, last_p + avg, confidence


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=HISTORY)
    ap.add_argument("--slug", help="only this event")
    ap.add_argument("--ladder", action="store_true", help="show observed price ladders")
    ap.add_argument("--min-available", type=int, default=0,
                    help="hide tiers with more than N left (0 = show all)")
    a = ap.parse_args()

    series = load_history(a.history)
    now = datetime.now(timezone.utc)

    all_rows = [r for rows in series.values() for r in rows]
    span = (max(r["_t"] for r in all_rows) - min(r["_t"] for r in all_rows))
    span_h = span.total_seconds() / 3600

    print(f"history: {len(all_rows):,} rows · {len(series)} tiers · "
          f"{span_h:.1f}h of data\n")

    if span_h < 6:
        print("  ⚠️  Less than 6h of history. Burn rates below are near-meaningless.")
        print("      Come back in a day or two.\n")

    # ------------------------------------------------------------- ladders
    if a.ladder:
        ladders = price_ladders(series)
        print("OBSERVED PRICE LADDERS (only releases the watcher has seen)\n")
        for (slug, _tid), lad in sorted(ladders.items()):
            if a.slug and slug != a.slug:
                continue
            if len(lad) < 2:
                continue          # a single observation is not a ladder
            rows = series[(slug, _tid)]
            base = RELEASE_RE.match(rows[-1]["name"])
            base = base.group("base") if base else rows[-1]["name"]
            chain = " → ".join(f"r{n} ${p:g}" for n, p in lad)
            print(f"  {slug}")
            print(f"    {base}: {chain}")
            proj = project_next(lad)
            if proj:
                n, p, conf = proj
                print(f"    → next likely r{n} ≈ ${p:.0f}  ({conf})")
            print()
        if not any(len(l) >= 2 for l in ladders.values()):
            print("  No tier has rolled over since logging began — no ladders yet.")
            print("  This fills in as releases turn over. Check back in a few days.\n")
        return

    # --------------------------------------------------------- burn rates
    print(f"{'tier':<44} {'left':>9}  {'1h':>5} {'6h':>5} {'24h':>5}  "
          f"{'spread':>6}  {'sells out':>9}")
    print("-" * 100)

    rows_out = []
    for (slug, tid), rows in series.items():
        if a.slug and slug != a.slug:
            continue
        info = analyze_tier(rows, now)
        if info["available"] <= 0:
            continue
        if a.min_available and info["available"] > a.min_available:
            continue
        rows_out.append((slug, info))

    # most urgent first
    rows_out.sort(key=lambda x: x[1]["eta_hours"] if x[1]["eta_hours"] else 9e9)

    for slug, i in rows_out:
        label = f"{slug[:22]:<22} {i['name'][:20]:<20}"
        spread = f"{i['spread']:.1f}×" if i["spread"] else "  —  "
        warn = ""
        if i["spread"] and i["spread"] > 3:
            warn = "  ← bursty, ETA unreliable"
        print(f"{label} {i['available']:>4}/{i['quantity']:<4} "
              f"{fmt_rate(i['r1'])} {fmt_rate(i['r6'])} {fmt_rate(i['r24'])}  "
              f"{spread:>6}  {fmt_eta(i['eta_hours']):>9}{warn}")

    print(f"""
READ THIS BEFORE TRUSTING ANY NUMBER ABOVE

  1h / 6h / 24h  = tickets sold per hour over that trailing window.
  spread         = max rate ÷ min rate. How much the windows DISAGREE.
  sells out      = available ÷ 6h rate. Linear. Naive on purpose.

  A spread under ~2x means selling is steady and the ETA is worth something.
  A spread over ~3x means it's bursty — the tier sold 8 tickets in ten minutes
  and nothing for six hours — and the ETA is a confident lie.

  Do NOT wire this into a Discord alert until you've watched a few tiers roll
  over and confirmed the rates hold up. A wrong ETA in a resale decision is
  worse than no ETA.
""")


if __name__ == "__main__":
    main()