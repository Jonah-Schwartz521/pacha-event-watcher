import requests
from .models import Event


def notify_discord(webhook_url: str, event: Event, dry_run: bool = False) -> None:
    if dry_run or not webhook_url.strip():
        print(f"[DRY RUN] Would send: {event.title} | {event.date} | {event.url}")
        return

    payload = {
        "content": (
            f"**New Concourse Project Event**\n"
            f"**{event.title}**\n"
            f"{event.date}\n"
            f"{event.url}"
        )
    }

    response = requests.post(webhook_url, json=payload, timeout=15)
    response.raise_for_status()
