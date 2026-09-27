"""Builds each client's daily plan: N videos (1-4), each with a generation queue
entry and a publish time, in the client's own timezone."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db

DEFAULT_SLOTS = ["09:00", "13:00", "18:00", "21:00"]


def parse_slot_times(text: str) -> list[str]:
    """'09:00,13:00,18:00,21:00' -> ['09:00','13:00','18:00','21:00']."""
    out = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = part.split(":")
            h, m = int(h), int(m)
            assert 0 <= h <= 23 and 0 <= m <= 59
        except (ValueError, AssertionError):
            raise ValueError(f"'{part}' is not a valid HH:MM time")
        out.append(f"{h:02d}:{m:02d}")
    return out


def client_tz(client) -> ZoneInfo:
    try:
        return ZoneInfo(client["timezone"] or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def today_str(client) -> str:
    return datetime.now(client_tz(client)).strftime("%Y-%m-%d")


def slots_for(client) -> list[tuple[int, str]]:
    """[(slot_number, 'HH:MM'), ...] for as many videos/day as configured."""
    n = max(1, min(4, int(client["videos_per_day"] or 1)))
    times = parse_slot_times(client["slot_times"]) or DEFAULT_SLOTS
    while len(times) < n:
        times.append(times[-1])
    return list(enumerate(sorted(times[:n]), start=1))


def publish_datetime(client, plan_date: str, hhmm: str) -> datetime:
    y, mo, d = (int(x) for x in plan_date.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    return datetime(y, mo, d, h, mi, tzinfo=client_tz(client))


def ensure_today_planned(client, log=print) -> int:
    """Creates any missing queued rows for today's slots. Returns how many were created."""
    date = today_str(client)
    existing = {r["slot"] for r in db.runs_for_plan(client["id"], date)}
    created = 0
    for slot, hhmm in slots_for(client):
        if slot in existing:
            continue
        publish_at = db.iso(publish_datetime(client, date, hhmm))
        rid = db.create_run(client["id"], trigger="plan", plan_date=date, slot=slot, publish_at=publish_at)
        if rid:
            created += 1
            log(f"{client['name']}: planned video {slot} for {date} {hhmm} {client['timezone']}")
    return created
