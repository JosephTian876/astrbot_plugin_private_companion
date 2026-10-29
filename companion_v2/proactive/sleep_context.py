"""A small, optional sleep view shared by proactive contact and wake-up paths.

The Huawei Health cloud record is completed *after* sleep, so it must never be
used as a real-time sleep sensor or as the alarm trigger.  This module only
projects the latest completed record into a conservative wake-relative window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_RECORD_AGE = timedelta(hours=36)
WAKE_GRACE = timedelta(minutes=20)
MORNING_WINDOW = timedelta(minutes=120)


def _event_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class SleepContext:
    event_id: str
    ended_at: datetime
    duration_minutes: float | None
    source_age_seconds: float

    @classmethod
    def from_events(cls, events, at: datetime):
        """Return the newest completed sleep event, or None when unusable."""
        if not isinstance(at, datetime) or at.tzinfo is None:
            return None
        candidates = []
        for event in events or ():
            if not isinstance(event, dict) or event.get("kind") != "sleep_completed":
                continue
            ended = _event_time(event.get("occurred_at"))
            if ended is None or ended > at + timedelta(minutes=10):
                continue
            age = (at - ended).total_seconds()
            if age < 0 or age > MAX_RECORD_AGE.total_seconds():
                continue
            facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
            duration = facts.get("duration_minutes")
            try:
                duration = round(float(duration), 1) if duration is not None else None
            except (TypeError, ValueError, OverflowError):
                duration = None
            candidates.append((ended, event.get("event_id") or "", duration, max(0.0, age)))
        if not candidates:
            return None
        ended, event_id, duration, age = max(candidates, key=lambda item: item[0])
        return cls(str(event_id), ended, duration, age)

    @property
    def qualifier(self):
        if self.duration_minutes is None:
            return "睡眠记录已完成"
        if self.duration_minutes < 360:
            return "最近一次睡眠偏短"
        if self.duration_minutes > 540:
            return "最近一次睡眠偏长"
        return "最近一次睡眠时长正常"

    @property
    def quiet_until(self):
        return self.ended_at + WAKE_GRACE

    @property
    def morning_opened_at(self):
        return self.ended_at + WAKE_GRACE

    @property
    def morning_expires_at(self):
        return self.ended_at + WAKE_GRACE + MORNING_WINDOW

    def morning_hint(self):
        return (self.qualifier +
                "；只能作为关心或问候的背景，不报具体数值、不诊断、不声称实时看见。")


def from_health_source(source, at: datetime):
    """Best-effort adapter for the optional Huawei Health event cache."""
    if source is None or not isinstance(at, datetime):
        return None
    try:
        events = source.cached_events(max_age_seconds=int(MAX_RECORD_AGE.total_seconds()))
    except Exception:
        return None
    return SleepContext.from_events(events, at)


__all__ = ["SleepContext", "from_health_source", "MAX_RECORD_AGE", "WAKE_GRACE", "MORNING_WINDOW"]
