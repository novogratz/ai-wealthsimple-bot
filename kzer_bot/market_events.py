"""Operator-maintained high-impact event blackout calendar."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")


def load_events(path: Path) -> list[datetime]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        events = [datetime.fromisoformat(item).astimezone(TZ) for item in raw.get("events", [])]
        return sorted(events)
    except Exception:
        return []


def event_blackout(
    now: datetime, path: Path, before_minutes: int = 30, after_minutes: int = 30,
) -> tuple[bool, str]:
    for event in load_events(path):
        if event - timedelta(minutes=before_minutes) <= now <= event + timedelta(minutes=after_minutes):
            return True, f"high-impact event blackout around {event:%Y-%m-%d %H:%M ET}"
    return False, ""
