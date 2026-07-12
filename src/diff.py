"""
The change-detection brain.

Compares freshly parsed events against saved state and emits Alerts. All the
noise-control lives here:

  * FIRST RUN      — seed everything silently, alert nothing.
  * NEW TIER       — record its current severity silently; a brand-new tier is
                     never "low stock", it just hasn't sold anything yet.
  * CROSSINGS      — alert when severity gets WORSE, never on every decrement.
                     A tier pings at most once per level, ever.
  * ROLLOVER       — new release = new allocation, so the latch resets and we
                     start watching the new release from scratch.
  * SANITY CAP     — see classify(); "almost gone" must mean *actually* almost
                     gone, not "small tier that sold one ticket".

Dry-run against live data (sends nothing):
    python -m src.diff
    python -m src.diff --classify      # just show how every tier classifies
"""
from __future__ import annotations

from dataclasses import dataclass

from .parse import Event, Tier
from .state import now_iso, severity_rank

STYLE = {
    "low": ("🟠", "LOW STOCK"),
    "critical": ("🔴", "ALMOST GONE"),
    "sold_out": ("🖤", "SOLD OUT"),
}

# The absolute threshold is capped at this fraction of the allocation, so a small
# tier can't trip CRITICAL while most of it is still unsold. Without this, a fresh
# 20-seat VIP tier selling ONE ticket (19 left, 19 <= 20) fires
# "ALMOST GONE — 19/20 left (95%)". Seen live on Alok VIP (12/20 = 60% left) and
# GORDO VIP (20/50 = 40% left) — neither was anywhere near gone.
CRITICAL_MAX_PCT = 0.35


def classify(t: Tier, low_abs: int, low_pct: float) -> str:
    """Which rung of the ladder is this tier on?  none < low < critical < sold_out"""
    if t.available <= 0:
        return "sold_out"

    if t.used <= 0:
        # Nothing has sold. A small allocation is not an emergency.
        return "none"

    # CRITICAL = few tickets left AND mostly sold through. BOTH must hold.
    critical_at = min(low_abs, t.quantity * CRITICAL_MAX_PCT) if t.quantity else low_abs
    if t.available <= critical_at:
        return "critical"

    # LOW = down to the last low_pct of a (necessarily larger) allocation.
    if t.quantity and (t.available / t.quantity) <= low_pct:
        return "low"

    return "none"


@dataclass
class Alert:
    kind: str          # new_event | rollover | price_change | new_tier | stock
    event: Event
    tier: Tier | None = None
    detail: str = ""
    severity: str = ""     # only for kind == "stock"
    priority: int = 1      # 0 = highest; controls send order


def _snapshot(t: Tier, severity: str) -> dict:
    return {
        "release_id": t.release_id,
        "name": t.name,
        "release_no": t.release_no,
        "price": t.price,
        "quantity": t.quantity,
        "available": t.available,
        "severity": severity,
    }


def diff(state: dict, events: list[Event], *, low_abs: int = 20,
         low_pct: float = 0.10, first_run_silent: bool = True) -> tuple[list[Alert], dict]:
    """Returns (alerts, updated_state). Pure — does not touch disk."""
    known = state.setdefault("events", {})
    seeding = first_run_silent and not known
    alerts: list[Alert] = []

    for e in events:
        prev_event = known.get(e.slug)

        # ---------------------------------------------------------- new event
        if prev_event is None:
            known[e.slug] = {
                "name": e.name,
                "date": e.date,
                "first_seen": now_iso(),
                # Seed at current severity — silently. We only alert on a
                # *worsening*, so a new event's already-low tier won't ping.
                "tiers": {t.tier_id: _snapshot(t, classify(t, low_abs, low_pct))
                          for t in e.tiers},
            }
            if not seeding:
                cheapest = min((t.price for t in e.tiers), default=0)
                alerts.append(Alert(
                    kind="new_event", event=e, priority=0,
                    detail=f"from ${cheapest:g} · {e.seats_left} seats",
                ))
            continue

        prev_tiers = prev_event.setdefault("tiers", {})
        prev_event["name"] = e.name
        prev_event["date"] = e.date
        seen_ids = set()

        for t in e.tiers:
            seen_ids.add(t.tier_id)
            prev = prev_tiers.get(t.tier_id)
            sev_now = classify(t, low_abs, low_pct)

            # ------------------------------------------------------- new tier
            if prev is None:
                prev_tiers[t.tier_id] = _snapshot(t, sev_now)
                if not seeding:
                    alerts.append(Alert(
                        kind="new_tier", event=e, tier=t, priority=1,
                        detail=f"${t.price:g} · {t.available}/{t.quantity}",
                    ))
                continue

            # -------------------------------------------------------- rollover
            # New release id => restocked at a new price point. Reset the latch,
            # or it stays pinned at "critical" forever and never warns again.
            if prev.get("release_id") != t.release_id:
                old_price, old_rel = prev.get("price"), prev.get("release_no")
                bits = []
                if old_rel and t.release_no:
                    bits.append(f"{old_rel} → {t.release_no} release")
                if old_price is not None and old_price != t.price:
                    arrow = "↑" if t.price > old_price else "↓"
                    bits.append(f"${old_price:g} {arrow} ${t.price:g}")
                bits.append(f"{t.available} available")
                alerts.append(Alert(kind="rollover", event=e, tier=t, priority=1,
                                    detail=" · ".join(bits)))
                prev_tiers[t.tier_id] = _snapshot(t, sev_now)
                continue

            # --------------------------- price change within the same release
            if prev.get("price") != t.price:
                old = prev.get("price")
                arrow = "↑" if t.price > (old or 0) else "↓"
                alerts.append(Alert(kind="price_change", event=e, tier=t, priority=1,
                                    detail=f"${old:g} {arrow} ${t.price:g}"))

            # ------------------------------------------------ stock escalation
            sev_prev = prev.get("severity", "none")
            if severity_rank(sev_now) > severity_rank(sev_prev):
                alerts.append(Alert(
                    kind="stock", event=e, tier=t, severity=sev_now,
                    priority=0 if sev_now in ("critical", "sold_out") else 1,
                    detail=f"{t.available}/{t.quantity} left ({t.pct_left:.0%}) · ${t.price:g}",
                ))
                prev["severity"] = sev_now
            # severity never ratchets back up without a rollover

            prev.update({
                "name": t.name,
                "release_no": t.release_no,
                "price": t.price,
                "quantity": t.quantity,
                "available": t.available,
            })

        # ----------------------------------------------- tier vanished entirely
        # anotr-11-07-2026 went 2 tiers -> 1, so sold-out tiers do sometimes get
        # pulled from the payload. Treat as a sellout — but don't double-ping.
        for tid, prev in list(prev_tiers.items()):
            if tid in seen_ids:
                continue
            if prev.get("severity") != "sold_out" and not seeding:
                alerts.append(Alert(
                    kind="stock", event=e, severity="sold_out", priority=0,
                    detail=f"{prev.get('name')} — tier removed from sale",
                ))
            prev["severity"] = "sold_out"

    alerts.sort(key=lambda a: a.priority)
    return alerts, state


def render(a: Alert) -> str:
    """Plain-text one-liner. The Discord formatter will do something richer."""
    when = (a.event.date or "")[:10]
    if a.kind == "new_event":
        return f"[Pacha] 🆕 NEW EVENT — {a.event.name} ({when}) · {a.detail}"
    if a.kind == "stock":
        emoji, label = STYLE.get(a.severity, ("⚠️", "STOCK"))
        tier = a.tier.base_name if a.tier else ""
        return f"[Pacha] {emoji} {label} — {a.event.name} {when} · {tier} · {a.detail}"
    if a.kind == "rollover":
        return f"[Pacha] 🔁 NEW RELEASE — {a.event.name} {when} · {a.tier.base_name} · {a.detail}"
    if a.kind == "price_change":
        return f"[Pacha] 💲 PRICE — {a.event.name} {when} · {a.tier.base_name} · {a.detail}"
    if a.kind == "new_tier":
        return f"[Pacha] ➕ NEW TIER — {a.event.name} {when} · {a.tier.name} · {a.detail}"
    return f"[Pacha] {a.kind} — {a.event.name}"


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import os

    from .parse import fetch_events
    from . import state as st

    ap = argparse.ArgumentParser(description="Dry-run the diff engine. Sends nothing.")
    ap.add_argument("--state", default=os.getenv("STATE_PATH", "data/pacha_state.json"))
    ap.add_argument("--low-abs", type=int, default=int(os.getenv("LOW_STOCK_ABS", "20")))
    ap.add_argument("--low-pct", type=float, default=float(os.getenv("LOW_STOCK_PCT", "0.10")))
    ap.add_argument("--save", action="store_true",
                    help="persist the new state (default: don't, so you can re-run)")
    ap.add_argument("--classify", action="store_true",
                    help="show how every live tier classifies right now (threshold tuning)")
    args = ap.parse_args()

    evs = fetch_events()

    # --classify: no state, no diffing. Just the ladder, for tuning thresholds.
    if args.classify:
        print(f"low_abs={args.low_abs}  low_pct={args.low_pct:.0%}  "
              f"critical capped at {CRITICAL_MAX_PCT:.0%} of allocation\n")
        for e in evs:
            rows = [(t, classify(t, args.low_abs, args.low_pct)) for t in e.tiers]
            if all(s == "none" for _, s in rows):
                continue
            print(f"{e.date[:10]}  {e.name}")
            for t, sev in rows:
                if sev == "none":
                    continue
                emoji, label = STYLE.get(sev, ("", sev))
                print(f"    {emoji} {label:<12} {t.base_name:<32} "
                      f"{t.available:>4}/{t.quantity:<5} ({t.pct_left:5.1%})  ${t.price:g}")
            print()
        raise SystemExit

    s = st.load(args.state)
    was_empty = not s.get("events")
    alerts, new_state = diff(s, evs, low_abs=args.low_abs, low_pct=args.low_pct)

    print(f"parsed {len(evs)} events · state had "
          f"{0 if was_empty else len(new_state['events'])} known\n")
    if was_empty:
        print("FIRST RUN — seeded silently, no alerts (this is correct).\n")
    elif not alerts:
        print("no changes since last run.\n")
    else:
        print(f"{len(alerts)} alert(s) would be sent:\n")
        for a in alerts:
            print("  " + render(a))
        print()

    if args.save:
        st.save(args.state, new_state)
        print(f"state saved -> {args.state}")
    else:
        print("(state NOT saved — pass --save to persist)")