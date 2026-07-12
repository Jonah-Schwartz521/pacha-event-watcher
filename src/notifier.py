"""
Discord webhook notifier.

Two things the Concourse version didn't have, and Pacha needs:

  * 429 HANDLING — Discord rate-limits webhooks (roughly 5 requests / 2s per
    webhook, and it's stricter than documented). A busy cycle here can produce a
    dozen alerts at once. On 429 we read `retry_after` from the JSON body and
    honour it, with a couple of retries before giving up on that message.

  * SPACING — a fixed sleep between consecutive posts so we don't walk into the
    429 in the first place. Cheaper than recovering from one.

Alerts are rendered as Discord embeds: colour-coded by severity, with the
checkout link as a clickable title. Falls back to plain content on failure.
"""
from __future__ import annotations

import time

import requests

from .diff import Alert, STYLE

# Discord embed colours (decimal)
COLOR = {
    "sold_out": 0x2B2D31,     # near-black
    "critical": 0xED4245,     # red
    "low": 0xFEE75C,          # yellow
    "new_event": 0x57F287,    # green
    "rollover": 0x5865F2,     # blurple
    "price_change": 0x5865F2,
    "new_tier": 0x57F287,
}

SEND_SPACING = 2.0        # seconds between consecutive posts
MAX_429_RETRIES = 3


def _title(a: Alert) -> str:
    if a.kind == "new_event":
        return f"🆕 NEW EVENT — {a.event.name}"
    if a.kind == "stock":
        emoji, label = STYLE.get(a.severity, ("⚠️", "STOCK"))
        return f"{emoji} {label} — {a.event.name}"
    if a.kind == "rollover":
        return f"🔁 NEW RELEASE — {a.event.name}"
    if a.kind == "price_change":
        return f"💲 PRICE CHANGE — {a.event.name}"
    if a.kind == "new_tier":
        return f"➕ NEW TIER — {a.event.name}"
    return f"{a.event.name}"


def _color(a: Alert) -> int:
    key = a.severity if a.kind == "stock" else a.kind
    return COLOR.get(key, 0x99AAB5)


def build_embed(a: Alert, prefix: str = "[Pacha]") -> dict:
    e = a.event
    when = (e.date or "")[:10]

    fields = []
    if a.tier:
        fields.append({
            "name": a.tier.name,
            "value": a.detail or "—",
            "inline": False,
        })
        if a.tier.checkout_url and not a.tier.sold_out:
            fields.append({
                "name": "Checkout",
                "value": f"[Buy now]({a.tier.checkout_url})",
                "inline": True,
            })
    elif a.detail:
        fields.append({"name": "Details", "value": a.detail, "inline": False})

    footer_bits = [when]
    if e.status:
        footer_bits.append(e.status)          # the venue's own "🔥SELLING FAST"
    if e.genres:
        footer_bits.append(", ".join(e.genres))

    embed = {
        "title": f"{prefix} {_title(a)}"[:256],
        "url": e.url,
        "color": _color(a),
        "fields": fields[:25],
        "footer": {"text": " · ".join(b for b in footer_bits if b)[:2048]},
    }
    if e.image:
        embed["thumbnail"] = {"url": e.image}
    return embed


def _post(webhook_url: str, payload: dict, log=print) -> bool:
    """POST once, honouring 429s. Returns True if delivered."""
    for attempt in range(1, MAX_429_RETRIES + 1):
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
        except requests.RequestException as ex:
            log("WARN", f"webhook post failed ({type(ex).__name__}: {ex})")
            return False

        if r.status_code == 429:
            # Discord tells us exactly how long to wait. Body is JSON:
            # {"message": "You are being rate limited.", "retry_after": 1.5, ...}
            try:
                wait = float(r.json().get("retry_after", 2.0))
            except (ValueError, requests.exceptions.JSONDecodeError):
                wait = float(r.headers.get("Retry-After", 2.0))
            wait = min(wait, 60.0) + 0.25          # small cushion
            log("WARN", f"rate limited by Discord, sleeping {wait:.1f}s "
                        f"(attempt {attempt}/{MAX_429_RETRIES})")
            time.sleep(wait)
            continue

        if r.status_code in (200, 204):
            return True

        if 500 <= r.status_code < 600:
            log("WARN", f"Discord {r.status_code}, retrying in 2s")
            time.sleep(2)
            continue

        # 4xx that isn't 429 — bad webhook, malformed embed. Retrying won't help.
        log("ERROR", f"Discord rejected the message: {r.status_code} {r.text[:300]}")
        return False

    log("ERROR", "gave up after repeated rate limiting")
    return False


def send(webhook_url: str, alerts: list[Alert], *, prefix: str = "[Pacha]",
         dry_run: bool = False, max_per_cycle: int = 15, log=print) -> int:
    """Send alerts, spaced out. Returns the number delivered."""
    if not alerts:
        return 0

    from .diff import render  # plain-text form, for dry-run + logging

    # A safety valve: if something goes badly wrong (bad state, site rewrite),
    # we'd rather drop alerts than dump 200 messages into someone's channel.
    if len(alerts) > max_per_cycle:
        log("WARN", f"{len(alerts)} alerts this cycle — capping at {max_per_cycle}. "
                    f"Alerts are sorted by priority, so the urgent ones survive.")
        alerts = alerts[:max_per_cycle]

    if dry_run or not webhook_url.strip():
        for a in alerts:
            log("DRY", render(a))
        return 0

    sent = 0
    for i, a in enumerate(alerts):
        if i:
            time.sleep(SEND_SPACING)
        ok = _post(webhook_url, {"embeds": [build_embed(a, prefix)]}, log=log)
        if ok:
            sent += 1
            log("INFO", f"sent: {render(a)}")
        else:
            log("ERROR", f"NOT sent: {render(a)}")
    return sent


def send_test(webhook_url: str, prefix: str = "[Pacha]", log=print) -> bool:
    """TEST_MODE: prove the webhook works without waiting for a real change."""
    payload = {"embeds": [{
        "title": f"{prefix} ✅ Test notification",
        "description": "Pacha NYC event watcher is wired up correctly.",
        "color": 0x57F287,
    }]}
    ok = _post(webhook_url, payload, log=log)
    log("INFO" if ok else "ERROR",
        "test notification delivered" if ok else "test notification FAILED")
    return ok