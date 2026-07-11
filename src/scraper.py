import os
import re
from typing import Callable, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .models import Event
import time

DEBUG_PARSE_HINT = (
    'docker compose exec watcher env DEBUG_PARSE=1 python -c "from src.scraper import '
    "fetch_html, parse_events; h=fetch_html('https://concourseproject.com/calendar/'); "
    "parse_events(h, base_url='https://concourseproject.com/calendar/')\""
)

CONTAINER_SELECTORS = (
    "div.seetickets-list-event-container",
    "div.seetickets-calendar-event-container",
)


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip().lower()


def _fallback_event_id(title: str, date: str, event_time: str) -> str:
    return _normalize_text(f"{title}|{date}|{event_time}")


def _debug_parse_enabled() -> bool:
    return os.getenv("DEBUG_PARSE", "").strip().lower() in {"1", "true", "yes", "y"}


def _short_class_tag(node) -> str:
    if not node:
        return "(none)"
    classes = ".".join(node.get("class", [])) if hasattr(node, "get") else ""
    return f"{node.name}.{classes}" if classes else str(node.name)


def _compact_html(node, limit: int = 1400) -> str:
    if not node:
        return ""
    compact = " ".join(str(node).split())
    return compact[:limit]


def _nearest_event_container(node):
    cur = node
    while cur:
        if getattr(cur, "name", None) == "div":
            class_set = set(cur.get("class", []))
            if {"seetickets-list-event-container", "seetickets-calendar-event-container"} & class_set:
                return cur
        cur = getattr(cur, "parent", None)
    return None


def _debug_probe_coming_soon_dom(soup: BeautifulSoup) -> None:
    # Debug run: docker compose exec watcher env DEBUG_PARSE=1 python -c "from src.scraper import fetch_html, parse_events; h=fetch_html('https://concourseproject.com/calendar/'); parse_events(h, base_url='https://concourseproject.com/calendar/')"
    keywords = ("Spencer Brown", "Qrion", "Massane")
    match = None
    found_keyword = None
    for keyword in keywords:
        match = soup.find(string=lambda s: s and keyword.lower() in s.lower())
        if match:
            found_keyword = keyword
            break

    if not match:
        print(
            "[DEBUG_PARSE] No keyword match for Spencer Brown/Qrion/Massane. "
            f"Hint: {DEBUG_PARSE_HINT}"
        )
        return

    element = match.parent
    print(f"[DEBUG_PARSE] Found keyword '{found_keyword}' in tag <{element.name}>")
    print("[DEBUG_PARSE] Parent chain (up to 6 levels):")
    cur = element
    for depth in range(6):
        if not cur:
            break
        print(f"[DEBUG_PARSE]   {depth}: {_short_class_tag(cur)}")
        cur = getattr(cur, "parent", None)

    container = _nearest_event_container(element)
    if container:
        print(f"[DEBUG_PARSE] Nearest event container: {_short_class_tag(container)}")
        print(f"[DEBUG_PARSE] Event container snippet: {_compact_html(container)}")
    else:
        print("[DEBUG_PARSE] No nearby event container found for keyword match")


def _get_containers(soup: BeautifulSoup):
    containers = []
    seen = set()
    for selector in CONTAINER_SELECTORS:
        for node in soup.select(selector):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            containers.append(node)
    return containers


def _extract_title(container) -> str:
    title_link = container.select_one("p.event-title a, .event-title a")
    if title_link:
        return title_link.get_text(strip=True)
    title_el = container.select_one("p.event-title, .event-title")
    return title_el.get_text(strip=True) if title_el else ""


def _extract_time(container) -> str:
    time_el = container.select_one("p.event-time, .event-time, .doortime-showtime")
    return time_el.get_text(strip=True) if time_el else ""


def _extract_date(container) -> str:
    date_el = container.select_one("p.event-date, .event-date")
    if date_el:
        return date_el.get_text(strip=True)

    ticket_like = container.select_one(".seetickets-buy-btn a[aria-label], .button-comingsoon[aria-label]")
    if ticket_like and ticket_like.get("aria-label"):
        aria = " ".join(ticket_like.get("aria-label").split())
        match = re.search(r"\bon\s+([A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?)", aria)
        if match:
            return match.group(1)

    date_num = container.find_parent("td")
    if date_num:
        day_el = date_num.select_one("div.date-number")
        if day_el:
            return day_el.get_text(strip=True)
    return ""


def _extract_ticket_href(container) -> str:
    title_link = container.select_one("p.event-title a[href], .event-title a[href]")
    if title_link and title_link.get("href"):
        return title_link.get("href")

    buy_link = container.select_one(".seetickets-buy-btn a[href]")
    if buy_link and buy_link.get("href"):
        return buy_link.get("href")
    return ""


def fetch_html(url: str, retries: int = 3, delay: int = 5) -> str:
    """
    Fetch HTML with retry + backoff.
    Retries on network errors or 5xx responses.
    """

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (concourse-event-watcher/1.0)"
                },
            )

            response.raise_for_status()
            return response.text

        except requests.RequestException as e:
            print(f"[WARN] Fetch attempt {attempt} failed: {e}")

            if attempt < retries:
                time.sleep(delay)
            else:
                raise


def parse_events(
    html: str,
    base_url: str,
    logger: Optional[Callable[[str, str], None]] = None,
) -> List[Event]:
    soup = BeautifulSoup(html, "html.parser")

    if _debug_parse_enabled():
        _debug_probe_coming_soon_dom(soup)

    containers = _get_containers(soup)

    events = []

    for idx, c in enumerate(containers, start=1):
        title = _extract_title(c)
        date = _extract_date(c)
        event_time = _extract_time(c)

        if not title or not date:
            if logger:
                missing = []
                if not title:
                    missing.append("title")
                if not date:
                    missing.append("date")
                logger("DEBUG", f"Skipping event #{idx}: missing {','.join(missing)}")
            continue

        href = _extract_ticket_href(c)
        event_url = urljoin(base_url, href) if href else ""

        if href:
            event_id = event_url
        else:
            event_id = _fallback_event_id(title, date, event_time)
            if logger:
                logger(
                    "DEBUG",
                    "Parsed event without ticket link "
                    f"(using fallback event_id): {title} | {date} | {event_time}",
                )

        if logger and c.select_one(".button-comingsoon"):
            logger(
                "DEBUG",
                f"Parsed coming-soon event: {title} | {date} | {event_time} | {event_id}",
            )

        if not event_id:
            if logger:
                logger("DEBUG", f"Skipping event #{idx}: missing event_id after parsing")
            continue

        events.append(
            Event(
                url=event_url,
                title=title,
                date=date,
                time=event_time,
                event_id=event_id,
            )
        )

    # Deduplicate by stable event_id
    unique = {e.event_id: e for e in events}
    if logger and len(unique) != len(events):
        logger("DEBUG", f"Deduplicated {len(events) - len(unique)} events by event_id")

    return list(unique.values())
