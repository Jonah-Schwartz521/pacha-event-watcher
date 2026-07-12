"""
Site-specific parsing layer for Pacha NYC.

Everything comes from ONE request to /events. The HTML has no event cards, but
the Next.js App Router RSC payload (self.__next_f.push chunks) carries a full
`initialEvents` array: every event, every tier, every price, and live stock
counts (quantity / used / available).

Detail pages are NOT used — they expose only a rendered "4 TICKETS LEFT!" string,
which is strictly less information than the listing's numeric `available`.

Run standalone to inspect what it sees:
    python -m src.parse
    python -m src.parse --json
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field

import requests

BASE = "https://pacha-nyc.com"
LISTING = f"{BASE}/events"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# "General Access - 17th Release" -> ("General Access", 17)
RELEASE_RE = re.compile(r"^(?P<base>.*?)\s*[-–]\s*(?P<n>\d+)(?:st|nd|rd|th)\s+Release\s*$", re.I)
FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', re.S)


class ParseError(RuntimeError):
    """Raised when the page loads but the payload isn't where we expect."""


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
@dataclass
class Tier:
    tier_id: str            # tickets[]._id — the PRODUCT, stable across releases
    release_id: str         # current_price._id — changes when a release rolls over
    name: str               # "General Access - 17th Release"
    base_name: str          # "General Access"
    release_no: int | None  # 17
    price: float
    quantity: int           # total allocation for this release
    used: int               # sold
    available: int          # remaining  (quantity - used)
    checkout_url: str

    @property
    def sold_out(self) -> bool:
        return self.available <= 0

    @property
    def pct_left(self) -> float:
        return (self.available / self.quantity) if self.quantity else 0.0


@dataclass
class Event:
    slug: str
    event_id: str
    name: str
    date: str               # ISO w/ tz, e.g. 2026-07-24T22:00:00-04:00
    url: str
    image: str | None
    status: str | None      # venue's own marquee, e.g. "🔥SELLING FAST"
    age: str | None
    genres: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    tiers: list[Tier] = field(default_factory=list)
    addons: list[dict] = field(default_factory=list)

    @property
    def seats_left(self) -> int:
        return sum(t.available for t in self.tiers)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def _flight_text(html: str) -> str:
    """Concatenate + unescape the RSC flight chunks into one big string."""
    parts = []
    for chunk in FLIGHT_RE.findall(html):
        try:
            parts.append(json.loads('"' + chunk + '"'))
        except json.JSONDecodeError:
            parts.append(chunk)
    return "".join(parts)


def _find_initial_events(text: str) -> list[dict]:
    """The flight payload isn't valid JSON as a whole, so decode just the
    object that owns `initialEvents`."""
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{"initialEvents"', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("initialEvents"), list):
            return obj["initialEvents"]
    return []


def _split_release(name: str) -> tuple[str, int | None]:
    m = RELEASE_RE.match(name or "")
    if not m:
        return (name or "").strip(), None
    return m.group("base").strip(), int(m.group("n"))


def _tier(raw: dict, slug: str) -> Tier | None:
    cp = raw.get("current_price") or {}
    if not cp:
        return None
    name = cp.get("name") or raw.get("name") or "?"
    base, rel = _split_release(name)
    qty = int(cp.get("quantity") or 0)
    used = int(cp.get("used") or 0)
    avail = cp.get("available")
    avail = int(avail) if avail is not None else max(qty - used, 0)
    tid = raw.get("_id") or ""
    return Tier(
        tier_id=tid,
        release_id=str(cp.get("_id") or ""),
        name=name,
        base_name=base,
        release_no=rel,
        price=float(cp.get("price") or 0),
        quantity=qty,
        used=used,
        available=avail,
        checkout_url=f"{BASE}/checkout/{slug}?ticketId={tid}",
    )


def _event(raw: dict) -> Event | None:
    slug = raw.get("slug")
    if not slug:
        return None
    prices = raw.get("prices") or {}

    tiers = [t for t in (_tier(x, slug) for x in prices.get("tickets") or []) if t]

    addons = []
    for a in (prices.get("add_ons") or {}).values():
        addons.append({
            "id": a.get("_id"),
            "label": a.get("label"),
            "price": a.get("price"),
            "was": a.get("fake_price"),
        })

    artists = [a.get("name") for a in raw.get("artists") or [] if a.get("name")]

    return Event(
        slug=slug,
        event_id=raw.get("event_id") or "",
        name=raw.get("name") or slug,
        # NB: never derive the date from the slug — real slugs include
        # "alok-01-08-20262" (CMS typo) and "zhu-on-the-move" (no date at all).
        date=raw.get("start_date") or raw.get("date") or "",
        url=f"{BASE}/event/{slug}",
        image=raw.get("image"),
        status=(raw.get("status") or None),
        age=raw.get("age"),
        genres=list(raw.get("music_genres") or []),
        artists=artists,
        tiers=tiers,
        addons=addons,
    )


def fetch_events(session: requests.Session | None = None, timeout: int = 20) -> list[Event]:
    """One request -> every event, tier, price and live stock count."""
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", UA)
    r = s.get(LISTING, timeout=timeout)
    r.raise_for_status()

    raw = _find_initial_events(_flight_text(r.text))
    if not raw:
        # The site changed shape. Fail loudly rather than silently reporting
        # "no events" — which would look identical to a cancelled calendar.
        raise ParseError(
            "initialEvents not found in RSC payload — the site's structure "
            "likely changed. Re-run probe.py."
        )

    events = [e for e in (_event(x) for x in raw) if e]
    events.sort(key=lambda e: e.date)
    return events


def to_dict(e: Event) -> dict:
    d = asdict(e)
    for t, td in zip(e.tiers, d["tiers"]):
        td["sold_out"] = t.sold_out
    return d


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="dump normalized JSON")
    args = ap.parse_args()

    evs = fetch_events()

    if args.json:
        print(json.dumps([to_dict(e) for e in evs], indent=2, ensure_ascii=False))
        raise SystemExit

    LOW_PCT, LOW_ABS = 0.10, 20
    print(f"{len(evs)} events\n")
    for e in evs:
        flag = f"  [{e.status}]" if e.status else ""
        print(f"{e.date[:10]}  {e.name}{flag}")
        print(f"            {e.url}  ({e.seats_left} seats left)")
        for t in e.tiers:
            if t.sold_out:
                mark = "SOLD OUT"
            elif t.available <= LOW_ABS:
                mark = "CRITICAL"
            elif t.pct_left <= LOW_PCT:
                mark = "LOW"
            else:
                mark = ""
            rel = f"r{t.release_no}" if t.release_no else "-"
            print(f"    {t.base_name:<28} {rel:<4} ${t.price:<7g} "
                  f"{t.available:>5}/{t.quantity:<5} ({t.pct_left:5.1%})  {mark}")
        print()