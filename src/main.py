import os
import random
import signal
import time
from datetime import datetime

from .scraper import fetch_html, parse_events
from .state import load_seen, save_seen
from .notifier import notify_discord


def log(level: str, message: str) -> None:
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} UTC [{level}] {message}")


def get_bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


# Global stop flag for graceful shutdown
STOP_REQUESTED = False


def _handle_shutdown(signum, frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log("INFO", f"Shutdown signal received ({signum}). Will stop after this cycle.")


def main() -> None:
    # Wire up signal handlers (Docker uses SIGTERM)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    calendar_url = os.getenv("CALENDAR_URL", "").strip()
    state_path = os.getenv("STATE_PATH", "/app/data/seen_events.json").strip()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    poll_seconds = int(os.getenv("POLL_SECONDS", "120"))
    first_run_silent = get_bool_env("FIRST_RUN_SILENT", "true")

    jitter_min = int(os.getenv("JITTER_MIN_SECONDS", "0"))
    jitter_max = int(os.getenv("JITTER_MAX_SECONDS", "0"))

    dry_run = not bool(webhook_url)

    # One-shot test mode: send a test message and exit
    test_mode = get_bool_env("TEST_MODE", "false")

    log("INFO", "Concourse Event Watcher starting")
    log("INFO", f"Calendar URL: {calendar_url}")
    log("INFO", f"State path: {state_path}")
    log("INFO", f"Polling every {poll_seconds} seconds")
    log("INFO", f"Webhook configured: {'YES' if webhook_url else 'NO (dry-run mode)'}")

    if jitter_min > 0 and jitter_max >= jitter_min:
        log("INFO", f"Jitter enabled: +{jitter_min}..{jitter_max} seconds")

    if test_mode:
        log("INFO", "TEST_MODE enabled: sending a test notification and exiting")
        from .models import Event
        test_event = Event(
            url=calendar_url or "https://concourseproject.com/calendar/",
            title="Test Notification (concourse-event-watcher)",
            date="N/A",
            event_id=calendar_url or "https://concourseproject.com/calendar/",
        )
        try:
            notify_discord(webhook_url, test_event, dry_run=dry_run)
            log("INFO", "Test notification sent (or printed in dry-run mode). Exiting.")
        except Exception as ex:
            log("ERROR", f"Test notification failed: {type(ex).__name__}: {ex}")
        return

    while True:
        global STOP_REQUESTED
        if STOP_REQUESTED:
            log("INFO", "Stop requested. Exiting main loop.")
            break

        try:
            seen = load_seen(state_path)

            html = fetch_html(calendar_url)
            events = parse_events(html, base_url=calendar_url, logger=log)

            log("INFO", f"Parsed {len(events)} events from calendar")
            log("INFO", f"Current known event count: {len(seen)}")

            current_event_ids = {e.event_id for e in events}

            if first_run_silent and not seen:
                save_seen(state_path, current_event_ids)
                log("INFO", f"[INIT] Seeded {len(current_event_ids)} existing events (no notifications sent)")
            else:
                new_events = [e for e in events if e.event_id not in seen]

                if new_events:
                    log("INFO", f"New events detected: {len(new_events)}")
                else:
                    log("INFO", "No new events detected")

                for event in new_events:
                    notify_discord(webhook_url, event, dry_run=dry_run)
                    log("INFO", f"Notified: {event.title} | {event.date}")
                    seen.add(event.event_id)

                save_seen(state_path, seen)
                log("INFO", f"State saved. Total tracked events: {len(seen)}")

        except Exception as ex:
            log("ERROR", f"{type(ex).__name__}: {ex}")

        sleep_time = poll_seconds
        if jitter_min > 0 and jitter_max >= jitter_min:
            sleep_time += random.randint(jitter_min, jitter_max)

        log("DEBUG", f"Sleeping for {sleep_time} seconds")

        # Sleep in small chunks so shutdown feels responsive
        slept = 0
        while slept < sleep_time and not STOP_REQUESTED:
            time.sleep(min(1, sleep_time - slept))
            slept += 1

    log("INFO", "Watcher stopped cleanly.")


if __name__ == "__main__":
    main()
