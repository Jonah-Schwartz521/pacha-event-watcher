"""
Discord webhook notifier.

Two things the Concourse version didn't have, and Pacha needs:

  * 429 HANDLING — Discord rate-limits webhooks harder than documented. A busy
    cycle here can produce a dozen alerts at once. On 429 we read `retry_after`
    from the JSON body and honour it.

  * SPACING — a fixed sleep between posts so we don't walk into the 429 at all.

@everyone PINGING
-----------------
PING_ON controls which alert types ping the channel. Default is all six.

A word of warning, since this is a resale channel people are meant to act on:
a ping that fires 15x a day gets muted, and then the ONE alert that mattered is
the one nobody sees. If that starts happening, don't turn pinging off entirely —
narrow it, e.g.:

    PING_ON=critical,rollover,new_event

Valid tokens: critical, sold_out, low, rollover, price_change, new_tier, new_event
Set PING_ON=none to disable pings without touching code.
"""
from __future__ import annotations

import os
import time

import requests

from .diff import Alert, STYLE

COLOR = {
    "sold_out": 0x2B2D31,
    "critical": 0xED4245,
    "low": 0xFEE75C,
    "new_event": 0x57F287,
    "rollover": 0x5865F2,
    "price_change": 0x5865F2,
    "new_tier": 0x57F287,
}

SEND_SPACING = 2.0
MAX_429_RETRIES = 3

ALL_KINDS = {"critical", "sold_out", "low", "rollover", "price_change",
             "new_tier", "new_event"}

_raw = os.getenv("PING_ON", "all").strip().lower()
if _raw in ("none", "off", ""):
    PING_ON: set[str] = set()
elif _raw == "all":
    PING_ON = set(ALL_KINDS)
else:
    PING_ON = {k.strip() for k in _raw.split(",") if k.strip() in ALL_KINDS}

PING_TEXT = os.getenv("PING_TEXT", "@everyone").strip()


def _ping_key(a: Alert) -> str:
    """Stock alerts ping by SEVERITY (critical vs low vs sold_out), everything
    else by kind. Lets you ping on 🔴 without also pinging on every 🟠."""
    return a.severity if a.kind == "stock" else a.kind


def should_ping(a: Alert) -> bool:
    return _ping_key(a) in PING_ON


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
    return a.event.name


def _color(a: Alert) -> int:
    return COLOR.get(_ping_key(a), 0x99AAB5)


def build_embed(a: Alert, prefix: str = "[Pacha]") -> dict:
    e = a.event
    when = (e.date or "")[:10]

    fields = []
    if a.tier:
        fields.append({"name": a.tier.name, "value": a.detail or "—", "inline": False})
        if a.tier.checkout_url and not a.tier.sold_out:
            fields.append({"name": "Checkout",
                           "value": f"[Buy now]({a.tier.checkout_url})", "inline": True})
    elif a.detail:
        fields.append({"name": "Details", "value": a.detail, "inline": False})

    footer = [when]
    if e.status:
        footer.append(e.status)
    if e.genres:
        footer.append(", ".join(e.genres))

    embed = {
        "title": f"{prefix} {_title(a)}"[:256],
        "url": e.url,
        "color": _color(a),
        "fields": fields[:25],
        "footer": {"text": " · ".join(b for b in footer if b)[:2048]},
    }
    if e.image:
        embed["thumbnail"] = {"url": e.image}
    return embed


def build_payload(a: Alert, prefix: str = "[Pacha]") -> dict:
    payload: dict = {"embeds": [build_embed(a, prefix)]}
    if should_ping(a):
        payload["content"] = PING_TEXT
        # Without this, Discord renders "@everyone" as plain text and nobody's
        # phone buzzes. The webhook must also have permission to mention everyone
        # in that channel — if pings render but don't notify, that's the culprit.
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    else:
        payload["allowed_mentions"] = {"parse": []}
    return payload


def _post(webhook_url: str, payload: dict, log=print) -> bool:
    for attempt in range(1, MAX_429_RETRIES + 1):
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
        except requests.RequestException as ex:
            log("WARN", f"webhook post failed ({type(ex).__name__}: {ex})")
            return False

        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2.0))
            except (ValueError, requests.exceptions.JSONDecodeError):
                wait = float(r.headers.get("Retry-After", 2.0))
            wait = min(wait, 60.0) + 0.25
            log("WARN", f"rate limited, sleeping {wait:.1f}s "
                        f"(attempt {attempt}/{MAX_429_RETRIES})")
            time.sleep(wait)
            continue

        if r.status_code in (200, 204):
            return True

        if 500 <= r.status_code < 600:
            log("WARN", f"Discord {r.status_code}, retrying in 2s")
            time.sleep(2)
            continue

        log("ERROR", f"Discord rejected the message: {r.status_code} {r.text[:300]}")
        return False

    log("ERROR", "gave up after repeated rate limiting")
    return False


def send(webhook_url: str, alerts: list[Alert], *, prefix: str = "[Pacha]",
         dry_run: bool = False, max_per_cycle: int = 15, log=print) -> int:
    if not alerts:
        return 0

    from .diff import render

    if len(alerts) > max_per_cycle:
        log("WARN", f"{len(alerts)} alerts this cycle — capping at {max_per_cycle}. "
                    f"Priority-sorted, so the urgent ones survive.")
        alerts = alerts[:max_per_cycle]

    if dry_run or not webhook_url.strip():
        for a in alerts:
            tag = " [@everyone]" if should_ping(a) else ""
            log("DRY", render(a) + tag)
        return 0

    sent = 0
    for i, a in enumerate(alerts):
        if i:
            time.sleep(SEND_SPACING)
        if _post(webhook_url, build_payload(a, prefix), log=log):
            sent += 1
            tag = " [@everyone]" if should_ping(a) else ""
            log("INFO", f"sent: {render(a)}{tag}")
        else:
            log("ERROR", f"NOT sent: {render(a)}")
    return sent


def send_test(webhook_url: str, prefix: str = "[Pacha]", log=print) -> bool:
    payload = {
        "content": PING_TEXT if PING_ON else None,
        "allowed_mentions": {"parse": ["everyone"] if PING_ON else []},
        "embeds": [{
            "title": f"{prefix} ✅ Test notification",
            "description": (f"Watcher is wired up. Pinging on: "
                            f"{', '.join(sorted(PING_ON)) if PING_ON else 'nothing'}."),
            "color": 0x57F287,
        }],
    }
    ok = _post(webhook_url, payload, log=log)
    log("INFO" if ok else "ERROR",
        "test notification delivered" if ok else "test notification FAILED")
    return ok