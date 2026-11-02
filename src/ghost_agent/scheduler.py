"""Schedules: recurring task creation for Ghost Agent.

A schedule is a store collection ("schedules") with:
  {id, goal, title, kind: "interval" | "daily",
   interval_minutes (interval kind), time_hhmm (daily kind, "HH:MM"
   in the server's local time), enabled, created_at, last_run_at,
   last_task_id, next_run_at}

tick(store, agent, now) is the whole engine and is pure with respect
to time: the caller injects `now`, which makes firing math fully
testable. The dashboard server calls tick every 20 seconds while it
serves; schedules therefore only fire while the dashboard server is
running.

Firing rules:
- Only enabled schedules whose next_run_at <= now fire.
- A schedule fires at most once per tick, however far behind it is:
  next_run_at advances from the previously scheduled time in whole
  intervals until it is strictly after now (missed intervals are
  skipped, never burst-fired).
- Firing means create + run a task through the normal agent loop, so
  governance is untouched: a scheduled write/exec step pauses at
  waiting_approval like any other run.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .store import Store, new_id

_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def parse_time_hhmm(value) -> tuple[int, int] | None:
    """Parse a strict 24h "HH:MM" string, or return None."""
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
        return None
    hh, mm = value.strip().split(":")
    return int(hh), int(mm)


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()  # naive input is read as local time
    return now


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def _next_daily(time_hhmm: str, now: datetime) -> datetime:
    parsed = parse_time_hhmm(time_hhmm) or (9, 0)
    cand = now.replace(hour=parsed[0], minute=parsed[1],
                       second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand


def _advance(schedule: dict, now: datetime) -> datetime:
    """Next run strictly after `now`, skipping any missed intervals."""
    if schedule.get("kind") == "daily":
        return _next_daily(schedule.get("time_hhmm") or "09:00", now)
    try:
        minutes = int(schedule.get("interval_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    step = timedelta(minutes=max(1, minutes))
    prev = _parse_dt(schedule.get("next_run_at"))
    nxt = (prev + step) if prev is not None else (now + step)
    while nxt <= now:
        nxt += step
    return nxt


def _initial_next(kind: str, interval_minutes, time_hhmm,
                  now: datetime) -> datetime:
    if kind == "daily":
        return _next_daily(time_hhmm, now)
    return now + timedelta(minutes=int(interval_minutes))


def create_schedule(store: Store, goal: str, kind: str = "interval",
                    interval_minutes=None, time_hhmm=None,
                    title: str | None = None, now: datetime | None = None,
                    enabled: bool = True) -> dict:
    """Validate and store a new schedule. Raises ValueError with a
    plain-language reason on bad input."""
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal is required")
    if kind not in ("interval", "daily"):
        raise ValueError("kind must be 'interval' or 'daily'")
    now = _aware(now)
    iv = None
    hhmm = None
    if kind == "interval":
        if isinstance(interval_minutes, bool):
            raise ValueError("interval_minutes must be a whole number of minutes")
        try:
            iv = int(interval_minutes)
        except (TypeError, ValueError):
            raise ValueError(
                "interval_minutes must be a whole number of minutes") from None
        if iv < 1:
            raise ValueError("interval_minutes must be at least 1")
    else:
        if parse_time_hhmm(time_hhmm or "") is None:
            raise ValueError("time_hhmm must be HH:MM (24h)")
        hhmm = (time_hhmm or "").strip()
    sched = {
        "id": new_id("sch"),
        "goal": goal,
        "title": (title or "").strip() or goal[:60],
        "kind": kind,
        "interval_minutes": iv,
        "time_hhmm": hhmm,
        "enabled": bool(enabled),
        "last_run_at": None,
        "last_task_id": None,
        "next_run_at": _initial_next(kind, iv, hhmm, now).isoformat(),
    }
    return store.put("schedules", sched)


def tick(store: Store, agent, now: datetime | None = None) -> list[str]:
    """Fire every due enabled schedule once. Returns fired ids."""
    now = _aware(now)
    fired: list[str] = []
    for sched in store.list("schedules"):
        if not sched.get("enabled"):
            continue
        nxt = _parse_dt(sched.get("next_run_at"))
        if nxt is not None and nxt > now:
            continue
        task = agent.create_task(sched["goal"], title=sched.get("title"))
        task = agent.run_task(task["id"])
        store.update("schedules", sched["id"],
                     last_run_at=now.isoformat(),
                     last_task_id=task["id"],
                     next_run_at=_advance(sched, now).isoformat())
        store.try_trail("schedule_fired",
                        {"schedule_id": sched["id"], "task_id": task["id"]},
                        task_id=task["id"])
        fired.append(sched["id"])
    return fired
