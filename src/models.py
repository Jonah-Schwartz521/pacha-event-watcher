from dataclasses import dataclass

@dataclass(frozen=True)
class Event:
    url: str
    title: str
    date: str
    time: str = ""
    event_id: str = ""
