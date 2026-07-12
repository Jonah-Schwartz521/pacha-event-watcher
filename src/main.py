"""
Pacha NYC event watcher — poll loop.

Skeleton (signals, jitter, chunked sleep, TEST_MODE, dry-run-when-no-webhook)
is carried over from the Concourse watcher. The middle is new:

    fetch_events()  ->  diff()  ->  notify()  ->  save state
    (one HTTP request per cycle — the listing carries everything)
"""
from __future__ import annotations

import os
import random
import signal
import time
from datetime import datetime, timezone

import requests

from . import state as st
from .diff import diff
from .notifier import send, send_test
from .parse import ParseError, fetch_events

STOP = False


def log(level: str, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} UTC [{level}] {message}", flush=True)


def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def _shutdown(signum, _frame) -> None:
    global STOP
    STOP = True
    log("INFO", f"Shutdown signal ({signum}). Stopping after this cycle.")


def fetch_with_retry(retries: int = 3, delay: int = 5):
    """Ported from the Concourse scraper's fetch_html. A blip shouldn't cost a
    whole cycle, but a persistent failure shouldn't spin either."""
    for attempt in range(1, retries + 1):
        try:
            return fetch_events()
        except (requests.RequestException, ParseError) as ex:
            log("WARN", f"fetch attempt {attempt}/{retries} failed: "
                        f"{type(ex).__name__}: {ex}")
            if attempt == retries:
                raise
            time.sleep(delay * attempt)      # linear backoff


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    prefix = os.getenv("ALERT_PREFIX", "[Pacha]").strip()
    state_path = os.getenv("STATE_PATH", "/app/data/pacha_state.json").strip()
    history_path = os.getenv("HISTORY_PATH", "/app/data/stock_history.jsonl").strip()

    poll_seconds = int(os.getenv("POLL_SECONDS", "300"))
    low_abs = int(os.getenv("LOW_STOCK_ABS", "20"))
    low_pct = float(os.getenv("LOW_STOCK_PCT", "0.10"))
    max_per_cycle = int(os.getenv("MAX_ALERTS_PER_CYCLE", "15"))

    first_run_silent = env_bool("FIRST_RUN_SILENT", "true")
    jitter_min = int(os.getenv("JITTER_MIN_SECONDS", "0"))
    jitter_max = int(os.getenv("JITTER_MAX_SECONDS", "30"))

    dry_run = env_bool("DRY_RUN", "false") or not webhook

    log("INFO", "Pacha NYC event watcher starting")
    log("INFO", f"State:      {state_path}")
    log("INFO", f"History:    {history_path or '(disabled)'}")
    log("INFO", f"Poll:       every {poll_seconds}s (+0..{jitter_max}s jitter)")
    log("INFO", f"Thresholds: critical <= {low_abs} (capped at 35% of allocation), "
                f"low <= {low_pct:.0%}")
    log("INFO", f"Webhook:    {'configured' if webhook else 'NONE — dry-run mode'}")

    if env_bool("TEST_MODE"):
        log("INFO", "TEST_MODE — sending a test notification and exiting")
        if dry_run:
            log("DRY", f"{prefix} ✅ Test notification (no webhook configured)")
        else:
            send_test(webhook, prefix, log=log)
        return

    while not STOP:
        try:
            events = fetch_with_retry()
            log("INFO", f"parsed {len(events)} events, "
                        f"{sum(len(e.tiers) for e in events)} tiers")

            state = st.load(state_path)
            seeding = first_run_silent and not state.get("events")

            alerts, state = diff(state, events, low_abs=low_abs, low_pct=low_pct,
                                 first_run_silent=first_run_silent)

            # History first: we want the datapoint even if notifying blows up.
            st.append_history(history_path, events)

            if seeding:
                log("INFO", f"[INIT] seeded {len(events)} events silently "
                            f"(no alerts — this is the first run)")
            elif alerts:
                log("INFO", f"{len(alerts)} change(s) detected")
                send(webhook, alerts, prefix=prefix, dry_run=dry_run,
                     max_per_cycle=max_per_cycle, log=log)
            else:
                log("INFO", "no changes")

            # Save AFTER notifying. If a send crashes, we'd rather re-alert next
            # cycle than silently swallow the change by having already latched it.
            st.save(state_path, state)

        except Exception as ex:
            log("ERROR", f"{type(ex).__name__}: {ex}")

        sleep_for = poll_seconds
        if jitter_max > jitter_min:
            sleep_for += random.randint(jitter_min, jitter_max)
        log("DEBUG", f"sleeping {sleep_for}s")

        slept = 0
        while slept < sleep_for and not STOP:
            time.sleep(1)
            slept += 1

    log("INFO", "Watcher stopped cleanly.")


if __name__ == "__main__":
    main()