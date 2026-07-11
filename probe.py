"""
probe.py — step-1 recon for the Pacha NYC scraper. Throwaway; not part of the app.

Run:
    docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
      bash -c "pip install -q requests && python probe.py --dump-raw"

Answers three questions:
  1. Where does the data live?  __NEXT_DATA__ blob, or App-Router RSC flight
     chunks (self.__next_f.push), or nowhere (HTML-only)?
  2. Do ticket tiers carry a RAW STOCK COUNT (quantity/remaining/available/...),
     or is the rendered "X left" label the only inventory signal?
  3. Is there an API endpoint the frontend calls that we could hit directly?

Everything downstream depends on these answers, so read the output before we
write a single line of the real parser.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

import requests

BASE = "https://pacha-nyc.com"
LISTING = f"{BASE}/events"
EXAMPLE = f"{BASE}/event/bunt-24-07-2026"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# keys that would mean "we can track real inventory"
STOCK_KEYS = ("quantity", "qty", "remaining", "remain", "stock", "inventory",
              "available", "avail", "capacity", "left", "sold", "count", "limit",
              "allocation", "max")

PRICEISH = re.compile(r"price|amount|cost|fee", re.I)
NAMEISH = re.compile(r"^(name|title|label|ticket_?name)$", re.I)
SLUG_RE = re.compile(r"/event/([a-z0-9][a-z0-9\-]*-\d{2}-\d{2}-\d{4})")
XLEFT_RE = re.compile(r"(\d+)\s*(?:tickets?\s+)?left", re.I)
API_RE = re.compile(
    r"https?://[^\s\"'\\<>]+?(?:/api/|convex\.cloud|\.execute-api\.|graphql)[^\s\"'\\<>]*", re.I)

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def fetch(url: str) -> str:
    r = session.get(url, timeout=20)
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------
# payload extraction
# --------------------------------------------------------------------------
def extract_next_data(html: str):
    """Pages Router / getServerSideProps style: <script id="__NEXT_DATA__">{...}"""
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def extract_flight(html: str) -> str:
    """App Router: data arrives as self.__next_f.push([1,"...escaped chunk..."]).

    Concatenate every chunk and unescape it back into (mostly) raw JSON text.
    """
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))   # unescape \" \n \uXXXX
        except json.JSONDecodeError:
            out.append(c)
    return "".join(out)


def iter_json_objects(text: str, must_contain: str = "price", cap: int = 40):
    """Pull standalone JSON objects out of a blob of flight text.

    The flight payload isn't valid JSON as a whole, so: try to decode an object
    at every '{' whose neighbourhood mentions the key we care about.
    """
    dec = json.JSONDecoder()
    found, i, n = [], 0, len(text)
    while i < n and len(found) < cap:
        i = text.find("{", i)
        if i < 0:
            break
        window = text[i:i + 4000]
        if must_contain and must_contain.lower() not in window.lower():
            i += 1
            continue
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, (dict, list)):
            found.append(obj)
            i = end
        else:
            i += 1
    return found


def walk(node, path="$"):
    """Yield (path, node) for every dict/list in the tree."""
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for idx, v in enumerate(node[:60]):
            yield from walk(v, f"{path}[{idx}]")


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def analyse(url: str, html: str, dump_raw: bool) -> None:
    print("\n" + "=" * 78)
    print(f"PROBE  {url}")
    print("=" * 78)
    print(f"  bytes: {len(html):,}")

    if dump_raw:
        os.makedirs("raw", exist_ok=True)
        fn = "raw/" + re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")[-60:] + ".html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  raw HTML -> {fn}   (eyeball this if the probe comes up empty)")

    nd = extract_next_data(html)
    flight = extract_flight(html)
    print(f"  __NEXT_DATA__ : {'YES' if nd else 'no'}")
    print(f"  RSC flight    : {len(flight):,} chars" if flight else "  RSC flight    : none")

    blobs = []
    if nd:
        blobs.append(("__NEXT_DATA__", nd))
    for i, o in enumerate(iter_json_objects(flight)):
        blobs.append((f"flight[{i}]", o))

    if not blobs:
        print("  !! no JSON payload found — parser would need HTML selectors")

    # --- ticket-tier-shaped objects (has a name AND a price) ---
    tiers = []
    for src, blob in blobs:
        for path, node in walk(blob):
            if isinstance(node, dict) and any(NAMEISH.match(k) for k in node) \
                    and any(PRICEISH.search(k) for k in node):
                tiers.append((src, path, node))

    print(f"\n  tier-shaped objects: {len(tiers)}")
    for src, path, node in tiers[:5]:
        print(f"\n  --- {src} @ {path}")
        body = json.dumps(node, indent=2, default=str)[:900]
        print("      " + body.replace("\n", "\n      "))

    if tiers:
        print("\n  keys across tier objects:")
        counts = Counter(k for _, _, t in tiers for k in t)
        for k, c in counts.most_common():
            hit = "   <-- STOCK?" if any(s in k.lower() for s in STOCK_KEYS) else ""
            print(f"    {k:<30} {c}{hit}")

    # --- any numeric stock-ish key anywhere ---
    print("\n  numeric stock-ish keys anywhere in payload:")
    hits = 0
    for src, blob in blobs:
        for path, node in walk(blob):
            if isinstance(node, dict):
                for k, v in node.items():
                    if any(s in k.lower() for s in STOCK_KEYS) and isinstance(v, (int, float)) \
                            and not isinstance(v, bool):
                        print(f"    {src} @ {path}.{k} = {v!r}")
                        hits += 1
    if not hits:
        print("    (none)")

    # --- the smoking gun: an "X left" label we can cross-reference ---
    for m in XLEFT_RE.finditer(html):
        print(f"\n  *** '{m.group(0)}' rendered in HTML — grep the JSON above for "
              f"{m.group(1)} to identify the stock field ***")

    apis = sorted(set(API_RE.findall(html)))[:12]
    if apis:
        print("\n  API endpoints referenced:")
        for a in apis:
            print(f"    {a}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=4,
                    help="how many event pages to probe (default 4)")
    ap.add_argument("--dump-raw", action="store_true",
                    help="save raw HTML to ./raw/ for manual inspection")
    a = ap.parse_args()

    try:
        listing = fetch(LISTING)
    except Exception as e:
        sys.exit(f"could not fetch {LISTING}: {e}")

    analyse(LISTING, listing, a.dump_raw)

    # Where do event slugs come from? The listing HTML looked client-rendered,
    # so they may only exist inside the flight payload — check both.
    slugs = list(dict.fromkeys(SLUG_RE.findall(listing) +
                               SLUG_RE.findall(extract_flight(listing))))
    print("\n" + "-" * 78)
    print(f"SLUGS DISCOVERABLE FROM /events: {len(slugs)}")
    for s in slugs[:30]:
        print(f"    {s}")
    if not slugs:
        print("    NONE — the listing gives us no slugs, so event discovery needs")
        print("    the API endpoint (see above) rather than this page.")

    targets = [f"{BASE}/event/{s}" for s in slugs[:a.events]] or [EXAMPLE]
    for i, url in enumerate(targets):
        if i:
            time.sleep(2)          # polite
        try:
            analyse(url, fetch(url), a.dump_raw)
        except Exception as e:
            print(f"\n  !! {url}: {e}")


if __name__ == "__main__":
    main()