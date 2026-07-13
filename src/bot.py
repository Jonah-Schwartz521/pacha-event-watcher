"""
Discord bot — lets people QUERY stock on demand, instead of only receiving alerts.

The webhook (notifier.py) is one-way: it can shout into the channel but can't
hear anything. This is a real bot: it holds an open connection and listens.

Commands:
    !low           tiers running low — SAME rule as the alerts
    !hot           what's actually moving right now
    !all           full sweep: every event, every tier, every count
    !stock <name>  one event
    !<name>        bare event name also works — !bunt, !black coffee

Runs as a SECOND container beside the watcher, sharing the same image and the
same ./data volume (it needs stock_history.jsonl for !hot).


A NOTE ON !hot — why there is no "sells out in ~2h"
---------------------------------------------------
The obvious design is seats_left ÷ sell_rate = ETA. It is also a lie whenever
sales are bursty: a tier that sold 8 tickets in ten minutes and nothing for six
hours produces a confident countdown built on one burst. A warning label doesn't
fix it — people read the number and ignore the caveat.

So !hot reports only OBSERVED FACTS: tickets lost in the last hour, and in the
last six. Those are true regardless of how the selling was distributed.


A NOTE ON AMBIGUOUS EVENTS
--------------------------
Pacha books the same artist twice — there are two Black Coffee shows (Sept 6 and
Oct 17). Returning just the first match would quietly show the wrong show's
inventory, which for a resale decision is worse than returning nothing. So a
multi-match lists ALL of them.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import discord

from .analyze import load_history
from .diff import classify
from .parse import fetch_events

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
PREFIX = os.getenv("BOT_PREFIX", "!").strip()
LOW_ABS = int(os.getenv("LOW_STOCK_ABS", "20"))
LOW_PCT = float(os.getenv("LOW_STOCK_PCT", "0.10"))
HISTORY = os.getenv("HISTORY_PATH", "/app/data/stock_history.jsonl")

# Don't hammer Pacha if someone spams commands. The data is only ~60s fresh
# anyway (that's the watcher's poll rate), so a 30s cache costs nothing real and
# protects the IP the watcher depends on.
CACHE_TTL = 30
_cache: dict = {"at": 0.0, "events": None}

COLOR_RED, COLOR_ORANGE, COLOR_BLUE, COLOR_GREY = 0xED4245, 0xFEE75C, 0x5865F2, 0x99AAB5
MARK = {"sold_out": "🖤", "critical": "🔴", "low": "🟠", "none": ""}

KNOWN_CMDS = {"low", "hot", "all", "stock", "help", "commands"}


def get_events(force: bool = False):
    now = time.time()
    if force or _cache["events"] is None or now - _cache["at"] > CACHE_TTL:
        _cache["events"] = fetch_events()
        _cache["at"] = now
    return _cache["events"]


def age_note() -> str:
    age = int(time.time() - _cache["at"])
    return "live" if age < 5 else f"{age}s ago"


def sold_since(rows: list[dict], hours: float, now: datetime) -> int | None:
    """Tickets this tier ACTUALLY LOST in the trailing window.

    Sums only decreases, so a release rollover (which resets `available` upward)
    doesn't register as negative sales.
    """
    cutoff = now - timedelta(hours=hours)
    window = [r for r in rows if r["_t"] >= cutoff]
    if len(window) < 2:
        return None
    sold = 0
    for a, b in zip(window, window[1:]):
        delta = a["available"] - b["available"]
        if delta > 0:
            sold += delta
    return sold


def find_events(query: str):
    """All events matching a loose name/slug query. Order preserved (by date)."""
    q = " ".join(query.lower().split())
    if not q:
        return []
    loose = q.replace(" ", "")
    out = []
    for e in get_events():
        hay_name = e.name.lower()
        hay_slug = e.slug.lower().replace("-", "")
        if q in hay_name or loose in hay_slug or loose in hay_name.replace(" ", ""):
            out.append(e)
    return out


# ---------------------------------------------------------------- rendering
def event_embed(e) -> discord.Embed:
    em = discord.Embed(title=e.name, url=e.url, color=COLOR_BLUE,
                       description=e.date[:10] + (f" · {e.status}" if e.status else ""))
    for t in e.tiers:
        mark = MARK.get(classify(t, LOW_ABS, LOW_PCT), "")
        link = f" · [buy]({t.checkout_url})" if t.available > 0 else ""
        em.add_field(
            name=f"{mark} {t.name}".strip(),
            value=f"**{t.available}/{t.quantity}** left ({t.pct_left:.0%}) · ${t.price:g}{link}",
            inline=False,
        )
    if e.image:
        em.set_thumbnail(url=e.image)
    em.set_footer(text=f"{e.seats_left} seats left · {age_note()}")
    return em


def cmd_stock(query: str) -> list[discord.Embed]:
    """Returns a LIST — an artist can play twice (two Black Coffee shows), and
    silently showing only the first would mean acting on the wrong show."""
    matches = find_events(query)

    if not matches:
        return [discord.Embed(
            title="No match",
            color=COLOR_GREY,
            description=(f"Nothing matching **{query}**.\n"
                         f"Try `{PREFIX}all` to see every event."),
        )]

    # Two shows for the same artist -> show both, clearly dated.
    if len(matches) > 1:
        head = discord.Embed(
            title=f"{len(matches)} events match “{query}”",
            color=COLOR_ORANGE,
            description="\n".join(f"• **{e.name}** — {e.date[:10]}" for e in matches),
        )
        return [head] + [event_embed(e) for e in matches[:3]]

    return [event_embed(matches[0])]


def cmd_low() -> discord.Embed:
    """Exactly the alert rule — so what he sees here matches what he gets pinged for."""
    rows = []
    for e in get_events():
        for t in e.tiers:
            sev = classify(t, LOW_ABS, LOW_PCT)
            if sev in ("critical", "low"):
                rows.append((sev, e, t))

    rows.sort(key=lambda r: (0 if r[0] == "critical" else 1, r[2].available))

    em = discord.Embed(
        title="🎫 Tiers running low",
        description=(f"Same rule as the alerts: 🔴 ≤{LOW_ABS} left (and mostly sold "
                     f"through) · 🟠 ≤{LOW_PCT:.0%} remaining"),
        color=COLOR_RED if any(r[0] == "critical" for r in rows) else COLOR_ORANGE,
    )
    if not rows:
        em.description = "Nothing is running low right now."
        em.color = COLOR_GREY
        return em

    for sev, e, t in rows[:24]:
        em.add_field(
            name=f"{MARK[sev]} {e.name} · {e.date[:10]}",
            value=(f"{t.name}\n**{t.available}/{t.quantity}** left "
                   f"({t.pct_left:.0%}) · ${t.price:g} · [buy]({t.checkout_url})"),
            inline=False,
        )
    em.set_footer(text=f"{len(rows)} tier(s) · {age_note()}")
    return em


def cmd_hot() -> discord.Embed:
    """What's moving RIGHT NOW. Observed deltas only — no projections.

    Ranked by pressure: tickets sold in 6h relative to what's LEFT. A tier with
    10 seats losing 5 is far more urgent than one with 500 losing 5.
    """
    events = get_events()
    try:
        series = load_history(HISTORY)
    except SystemExit:
        em = discord.Embed(title="🔥 Moving now", color=COLOR_GREY)
        em.description = "No history yet — the watcher needs to run a while first."
        return em

    now = datetime.now(timezone.utc)
    live = {(e.slug, t.tier_id): (e, t) for e in events for t in e.tiers}

    rows = []
    for key, hist in series.items():
        if key not in live:
            continue
        e, t = live[key]
        if t.available <= 0:
            continue
        s6 = sold_since(hist, 6, now)
        s1 = sold_since(hist, 1, now)
        if not s6:
            continue                       # sold nothing in 6h — not moving
        rows.append((s6 / max(t.available, 1), s6, s1, e, t))

    rows.sort(key=lambda r: -r[0])

    em = discord.Embed(
        title="🔥 Moving now",
        description="Tickets actually sold, last 6h and last 1h. Measured, not predicted.",
        color=COLOR_BLUE,
    )
    if not rows:
        em.description = "Nothing has sold in the last 6 hours."
        em.color = COLOR_GREY
        return em

    for _p, s6, s1, e, t in rows[:12]:
        recent = f" · **{s1} in the last hour**" if s1 else " · *quiet this hour*"
        em.add_field(
            name=f"{e.name} · {e.date[:10]}",
            value=(f"{t.name}\n**{t.available}/{t.quantity}** left · ${t.price:g}\n"
                   f"**−{s6} in 6h**{recent} · [buy]({t.checkout_url})"),
            inline=False,
        )
    em.set_footer(text=f"ranked by how fast stock is being eaten · {age_note()}")
    return em


def cmd_all() -> list[discord.Embed]:
    """Full sweep. Split across embeds — Discord caps at 25 fields each."""
    events = get_events()
    embeds, em, n = [], None, 0

    for e in events:
        if em is None or n >= 20:
            em = discord.Embed(
                title="🎟️ Full stock sweep" if not embeds else "🎟️ …continued",
                color=COLOR_BLUE)
            embeds.append(em)
            n = 0

        lines = []
        for t in e.tiers:
            mark = MARK.get(classify(t, LOW_ABS, LOW_PCT), "")
            lines.append(f"{mark} {t.base_name} — **{t.available}/{t.quantity}** · ${t.price:g}")

        flag = f" · {e.status}" if e.status else ""
        em.add_field(name=f"{e.name} · {e.date[:10]}{flag}",
                     value="\n".join(lines) or "no tiers", inline=False)
        n += 1

    total = sum(e.seats_left for e in events)
    embeds[-1].set_footer(
        text=f"{len(events)} events · {sum(len(e.tiers) for e in events)} tiers · "
             f"{total:,} seats left · {age_note()}")
    return embeds


def cmd_help() -> discord.Embed:
    em = discord.Embed(title="Pacha watcher — commands", color=COLOR_BLUE)
    em.add_field(name=f"{PREFIX}low", inline=False,
                 value="Tiers running low. Same rule the alerts use.")
    em.add_field(name=f"{PREFIX}hot", inline=False,
                 value="What's actually moving — tickets sold in the last 6h / 1h.")
    em.add_field(name=f"{PREFIX}all", inline=False,
                 value="Every event, every tier, every count.")
    em.add_field(name=f"{PREFIX}<event>", inline=False,
                 value=(f"Any event by name — `{PREFIX}bunt`, `{PREFIX}black coffee`, "
                        f"`{PREFIX}artbat`. (`{PREFIX}stock bunt` works too.)"))
    return em


# ------------------------------------------------------------------- client
intents = discord.Intents.default()
intents.message_content = True      # REQUIRED — and must ALSO be enabled in the
                                    # Discord developer portal, or the bot connects
                                    # fine and then silently ignores every message.

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"bot online as {client.user}", flush=True)


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.content.startswith(PREFIX):
        return

    body = msg.content[len(PREFIX):].strip()
    if not body:
        return

    parts = body.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    try:
        async with msg.channel.typing():
            if cmd == "low":
                await msg.channel.send(embed=cmd_low())

            elif cmd == "hot":
                await msg.channel.send(embed=cmd_hot())

            elif cmd == "all":
                for em in cmd_all():
                    await msg.channel.send(embed=em)

            elif cmd == "stock":
                if not arg:
                    await msg.channel.send(f"Usage: `{PREFIX}stock bunt`")
                else:
                    for em in cmd_stock(arg):
                        await msg.channel.send(embed=em)

            elif cmd in ("help", "commands"):
                await msg.channel.send(embed=cmd_help())

            else:
                # Not a known command -> treat the whole thing as an event name.
                # People naturally type "!black coffee", not "!stock black coffee",
                # and silently ignoring them is the worst possible response: they
                # can't tell if they typo'd or the bot is dead.
                for em in cmd_stock(body):
                    await msg.channel.send(embed=em)

    except Exception as ex:
        print(f"[ERROR] {cmd}: {type(ex).__name__}: {ex}", flush=True)
        await msg.channel.send(f"⚠️ `{cmd}` failed: {type(ex).__name__}")


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN not set — add it to .env")
    client.run(TOKEN)


if __name__ == "__main__":
    main()