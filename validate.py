"""
validate.py — step-2 recon. Decides whether /events alone is enough.

Run:
    docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
      bash -c "pip install -q requests && python validate.py"

Three questions:
  A. FRESHNESS  — is the RSC payload edge-cached (stale stock) or live?
  B. AGREEMENT  — does the listing's `available`/`price` for BUNT match what the
                  detail page renders ("17th Release, $170, 4 TICKETS LEFT")?
  C. COMPLETENESS — is initialEvents the full calendar, or just page 1?
"""
from __future__ import annotations

import json
import re
import time

import requests

BASE = "https://pacha-nyc.com"
LISTING = f"{BASE}/events"
DETAIL = f"{BASE}/event/bunt-24-07-2026"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

CACHE_HEADERS = ("age", "cache-control", "x-vercel-cache", "x-nextjs-cache",
                 "cf-cache-status", "x-cache", "date", "expires", "etag", "last-modified")


def flight(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))
        except json.JSONDecodeError:
            out.append(c)
    return "".join(out)


def find_initial_events(text: str):
    """Locate the object that owns initialEvents and decode it properly."""
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{"initialEvents"', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("initialEvents"), list):
            return obj["initialEvents"]
    return []


def tiers_of(ev):
    for t in (ev.get("prices") or {}).get("tickets") or []:
        cp = t.get("current_price") or {}
        yield {
            "tier_id": t.get("_id"),
            "name": cp.get("name") or t.get("name"),
            "price": cp.get("price"),
            "quantity": cp.get("quantity"),
            "used": cp.get("used"),
            "available": cp.get("available"),
        }


# ---------------------------------------------------------------- A. freshness
print("=" * 78)
print("A. FRESHNESS — is the listing edge-cached?")
print("=" * 78)

r1 = s.get(LISTING, timeout=20)
print(f"  status {r1.status_code}, {len(r1.text):,} bytes")
for h in CACHE_HEADERS:
    if h in r1.headers:
        print(f"    {h}: {r1.headers[h]}")

events1 = find_initial_events(flight(r1.text))
print(f"\n  initialEvents decoded: {len(events1)}")

# Fetch again after a pause with a cache-buster. If the payload is identical
# byte-for-byte AND `age` climbs, we're being served a cached page.
print("\n  refetching in 5s (cache-busted) to compare...")
time.sleep(5)
r2 = s.get(LISTING, params={"_cb": int(time.time())},
           headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=20)
for h in ("age", "x-vercel-cache", "x-nextjs-cache", "cf-cache-status", "date"):
    if h in r2.headers:
        print(f"    {h}: {r2.headers[h]}")

events2 = find_initial_events(flight(r2.text))
same = json.dumps(events1, sort_keys=True) == json.dumps(events2, sort_keys=True)
print(f"  payload identical across the two fetches: {same}")
print("    (identical is EXPECTED if nothing sold in 5s — this is only damning")
print("     if `age` is large or cache status says HIT with a long max-age)")

# ---------------------------------------------------------------- B. agreement
print("\n" + "=" * 78)
print("B. AGREEMENT — listing vs detail page for BUNT")
print("=" * 78)

bunt = next((e for e in events1 if e.get("slug") == "bunt-24-07-2026"), None)
if not bunt:
    print("  !! bunt-24-07-2026 NOT in initialEvents — listing may be incomplete")
    print("     slugs present:", [e.get("slug") for e in events1])
else:
    print("  LISTING says:")
    for t in tiers_of(bunt):
        print(f"    {t['name']:<42} ${t['price']:<6} "
              f"avail={t['available']}/{t['quantity']} (used {t['used']})")

    time.sleep(2)
    d = s.get(DETAIL, timeout=20).text
    print("\n  DETAIL PAGE says:")
    for m in re.finditer(
            r'\{"id":"[a-z0-9]{20,}","name":"([^"]+)","price":(\d+(?:\.\d+)?)'
            r'.*?"tag":"([^"]*)","soldOut":(true|false)\}', flight(d)):
        name, price, tag, sold = m.groups()
        tag = tag.encode().decode("unicode_escape") if "\\u" in tag else tag
        tag = "" if tag == "$undefined" else tag
        print(f"    {name:<42} ${price:<6} tag={tag!r} soldOut={sold}")
    print("\n  >>> Do the release numbers, prices, and the 'N TICKETS LEFT' number")
    print("      match the listing's `available`? That's the whole ballgame.")

# ------------------------------------------------------------ C. completeness
print("\n" + "=" * 78)
print("C. COMPLETENESS — is initialEvents the whole calendar?")
print("=" * 78)
print(f"  {len(events1)} events\n")
print(f"  {'slug':<34} {'date':<26} {'status':<10} tiers")
for e in sorted(events1, key=lambda x: str(x.get("start_date") or x.get("date"))):
    ts = list(tiers_of(e))
    tot = sum(t["available"] or 0 for t in ts)
    print(f"  {str(e.get('slug')):<34} {str(e.get('start_date') or e.get('date'))[:25]:<26} "
          f"{str(e.get('status')):<10} {len(ts)} tiers, {tot} seats left")
print("\n  >>> Compare this list against the site's calendar with SHOW MORE clicked.")
print("      If the site shows events this list doesn't, we need a pagination call.")