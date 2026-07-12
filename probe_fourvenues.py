"""
probe_fourvenues.py — does the ticketing backend expose FUTURE releases?

Pacha's own payload only ships `current_price` — the active release. We can see
"General Access - 17th Release, $170, 4 left" but nothing about the 18th.

Pacha is a front-end for FourVenues (every event carries an `iframe` field:
https://fourvenues.com/iframe/pacha-new-york/BPDH). If FourVenues exposes the
full price ladder — including unreleased tiers — we get forward visibility.
If it doesn't, that door is closed and we stop wondering.

Run:
    docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
      bash -c "pip install -q requests && python probe_fourvenues.py"
"""
from __future__ import annotations

import json
import re
import sys

import requests

from src.parse import _find_initial_events, _flight_text  # reuse our parser

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept": "*/*",
                  "Referer": "https://pacha-nyc.com/"})

# Keys that would mean "this is a future release we can't currently see"
FUTURE_HINTS = ("valid_from", "valid_to", "starts_at", "available_from", "release",
                "next", "upcoming", "scheduled", "future", "prices", "tiers")


def walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node[:40]):
            yield from walk(v, f"{path}[{i}]")


def show(label: str, r: requests.Response, limit: int = 2500) -> None:
    print(f"\n  {label}")
    print(f"    status {r.status_code} · {r.headers.get('content-type','?')} · {len(r.content):,}b")
    body = r.text[:limit]
    print("    " + body.replace("\n", "\n    ")[:limit])


# ---------------------------------------------------------------- find iframes
print("=" * 78)
print("STEP 1 — pull the FourVenues iframe URLs out of Pacha's payload")
print("=" * 78)

html = s.get("https://pacha-nyc.com/events", timeout=20).text
events = _find_initial_events(_flight_text(html))

iframes = {}
for e in events:
    if e.get("iframe"):
        iframes[e["slug"]] = e["iframe"]

print(f"  {len(iframes)} events carry an iframe URL")
for slug, url in list(iframes.items())[:5]:
    print(f"    {slug:<40} {url}")

if not iframes:
    sys.exit("no iframe URLs found — nothing to probe")

# Pick an event with an ACTIVE multi-release tier — BUNT has GA on its 17th
# release, so its ladder (if exposed) will be long and obvious.
target_slug = "bunt-24-07-2026" if "bunt-24-07-2026" in iframes else next(iter(iframes))
iframe_url = iframes[target_slug]
code = iframe_url.rstrip("/").split("/")[-1]      # e.g. "BPDH"

print(f"\n  probing: {target_slug}")
print(f"  iframe:  {iframe_url}")
print(f"  code:    {code}")

# ------------------------------------------------------------ fetch the iframe
print("\n" + "=" * 78)
print("STEP 2 — fetch the iframe itself")
print("=" * 78)

try:
    r = s.get(iframe_url, timeout=20)
    show("iframe HTML", r, 1500)
    iframe_html = r.text
except requests.RequestException as ex:
    print(f"  !! {ex}")
    iframe_html = ""

# Any JSON blobs / API calls referenced inside it?
if iframe_html:
    apis = sorted(set(re.findall(
        r'https?://[^\s"\'\\<>]+?(?:/api/|/v\d/|\.json)[^\s"\'\\<>]*', iframe_html)))
    print(f"\n  API-ish URLs referenced inside the iframe: {len(apis)}")
    for a in apis[:20]:
        print(f"    {a}")

    # Next.js / Nuxt / inline state?
    for pat, label in [
        (r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', "__NEXT_DATA__"),
        (r'window\.__NUXT__\s*=\s*(\{.*?\});', "__NUXT__"),
        (r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', "__INITIAL_STATE__"),
    ]:
        m = re.search(pat, iframe_html, re.S)
        if m:
            print(f"\n  found {label} in iframe")
            try:
                blob = json.loads(m.group(1))
                for path, node in walk(blob):
                    if isinstance(node, dict) and "price" in str(node)[:200].lower():
                        print(f"    {path}: {json.dumps(node, default=str)[:400]}")
                        break
            except json.JSONDecodeError:
                print("    (not parseable as JSON)")

# --------------------------------------------------- guess the public REST API
print("\n" + "=" * 78)
print("STEP 3 — try FourVenues' likely public endpoints")
print("=" * 78)
print("  (404s here are EXPECTED and fine — we're fishing)\n")

candidates = [
    f"https://fourvenues.com/api/events/{code}",
    f"https://fourvenues.com/api/v1/events/{code}",
    f"https://api.fourvenues.com/events/{code}",
    f"https://api.fourvenues.com/v1/events/{code}",
    f"https://fourvenues.com/api/iframe/pacha-new-york/{code}",
    f"https://fourvenues.com/pacha-new-york/{code}",
]

for url in candidates:
    try:
        r = s.get(url, timeout=15)
    except requests.RequestException as ex:
        print(f"  ✗ {url}\n      {type(ex).__name__}")
        continue

    ok = r.status_code == 200 and "json" in r.headers.get("content-type", "")
    mark = "★ JSON" if ok else ("· 200 (html)" if r.status_code == 200 else f"✗ {r.status_code}")
    print(f"  {mark:<14} {url}")

    if ok:
        try:
            data = r.json()
        except ValueError:
            continue
        print("\n      >>> JSON RESPONSE — hunting for a release ladder <<<")
        hits = 0
        for path, node in walk(data):
            if isinstance(node, dict) and any(h in k.lower() for k in node for h in FUTURE_HINTS):
                print(f"      {path}")
                print("        " + json.dumps(node, indent=2, default=str)[:700]
                      .replace("\n", "\n        "))
                hits += 1
                if hits >= 4:
                    break
        if not hits:
            print("      " + json.dumps(data, indent=2, default=str)[:1200]
                  .replace("\n", "\n      "))

print("\n" + "=" * 78)
print("WHAT TO LOOK FOR")
print("=" * 78)
print("""
  We want a tier object containing MORE THAN ONE price entry — a ladder like:

      "prices": [
        {"name": "16th Release", "price": 160, "quantity": 20, "used": 20},
        {"name": "17th Release", "price": 170, "quantity": 20, "used": 16},
        {"name": "18th Release", "price": 180, "quantity": 300, "used": 0}   <-- FUTURE
      ]

  Pacha's own payload gives us ONLY the middle one (`current_price`). If
  FourVenues hands over the whole array — including entries with used=0 and a
  future `valid_from` — we get the next release's price and allocation BEFORE
  it goes live. That's the win.

  If every endpoint 404s or needs auth, the door is closed: we forecast from
  our own stock_history.jsonl velocity data instead.
""")