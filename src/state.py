import json
from pathlib import Path
from typing import Set


def load_seen(path: str) -> Set[str]:
    p = Path(path)

    if not p.exists():
        return set()

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(data)
    except Exception:
        return set()

    return set()


def save_seen(path: str, seen: Set[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")
