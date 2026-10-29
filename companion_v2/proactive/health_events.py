"""Completed Huawei Health records as optional, source-backed contact material.

The producer does not create a health checker.  It turns one recent completed
sleep or workout record into a normal ``care`` opportunity; the existing
contact worker still decides whether the character should speak at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from companion_v2.conversation.messages import source_from_document
from companion_v2.execution.models import TaskContext, TaskError, digest, encode, require, stamp
from companion_v2.foundation.refs import ScopeKind, SourceRef, VersionRef
from companion_v2.sources.governance import bind
from companion_v2.sources.models import SourceRecord

from .contact_models import ContactIntent
from .models import EvidenceSnapshot, Opportunity, Thought, ThoughtKind


SOURCE_OWNER = "proactive.huawei_health"
MAX_EVENT_AGE = timedelta(hours=36)
SLEEP_OPEN_DELAY = timedelta(minutes=20)
SLEEP_WINDOW = timedelta(minutes=120)
WORKOUT_WINDOW = timedelta(minutes=120)
TREND_WINDOW = timedelta(hours=6)
WEEKLY_WINDOW = timedelta(hours=12)


class HealthContactEvents:
    """Expose recent completed Huawei Health events to the contact planner."""

    def __init__(self, contacts, source=None, settings_provider=None):
        self.contacts, self.source = contacts, source
        self.settings_provider = settings_provider
        self.runtime = contacts.runtime
        self.guard = bind(self.runtime, SOURCE_OWNER, self._validate_source)

    def update_source(self, source):
        self.source = source

    def close(self):
        self.guard.close()
        self.source = None

    def _values(self, context, at):
        if self.source is None or context.scope.kind is not ScopeKind.PRIVATE:
            return None
        try:
            values = self.settings_provider(context, at) if callable(self.settings_provider) else None
        except Exception:
            return None
        if not isinstance(values, dict) or values.get("health_enabled") is not True \
                or values.get("health_proactive_enabled") is not True:
            return None
        subject = getattr(getattr(context, "actor", None), "subject", None)
        role = getattr(getattr(context, "role_binding", None), "role", None)
        if subject is None or role is None or not self.source.is_bound() \
                or not self.source.allowed_for(subject.subject_id, getattr(role, "subject_id", "")):
            return None
        return values

    @staticmethod
    def _window(kind, occurred):
        if kind == "sleep_completed":
            return occurred + SLEEP_OPEN_DELAY, occurred + SLEEP_OPEN_DELAY + SLEEP_WINDOW
        if kind == "workout_completed":
            return occurred, occurred + WORKOUT_WINDOW
        if kind == "recovery_trend":
            return occurred, occurred + TREND_WINDOW
        return occurred, occurred + WEEKLY_WINDOW

    @staticmethod
    def _event_time(value):
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else None

    def _candidate_event(self, context, values, at, preferences):
        events = self.source.cached_events(max_age_seconds=int(MAX_EVENT_AGE.total_seconds()))
        for event in events:
            if not isinstance(event, dict) or event.get("kind") not in (
                    "sleep_completed", "workout_completed", "recovery_trend", "weekly_review"):
                continue
            occurred = self._event_time(event.get("occurred_at"))
            if occurred is None or occurred > at + timedelta(minutes=10):
                continue
            if at - occurred > MAX_EVENT_AGE:
                continue
            facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
            if event["kind"] == "sleep_completed":
                if facts.get("duration_minutes") is not None and facts["duration_minutes"] < 180:
                    continue
                purpose = "greeting"
                duration = facts.get("duration_minutes")
                qualifier = ""
                if isinstance(duration, (int, float)):
                    if duration < 360:
                        qualifier = "这次记录偏短，语气可以更轻一点。"
                    elif duration > 540:
                        qualifier = "这次记录偏长，不要拿时长打趣。"
                summary = (
                    "健康背景：昨晚的睡眠记录已经完成，可以作为一句自然问候、打趣或承接话题的背景。"
                    + qualifier +
                    "如果开口，只顺口提一点感受，不要逐项报睡眠分数或时长；不诊断、不催作息，也不要求主动提起。"
                )
            elif event["kind"] == "workout_completed":
                purpose = "care"
                pieces = []
                if facts.get("activity_type"):
                    pieces.append(str(facts["activity_type"]))
                if facts.get("distance_km") is not None:
                    pieces.append("%s 公里" % facts["distance_km"])
                if facts.get("duration_minutes") is not None:
                    pieces.append("%s 分钟" % facts["duration_minutes"])
                detail = "，".join(pieces)
                summary = (
                    "健康背景：最近完成了一次运动%s。可以像熟人一样顺口问一句、分享自己的感受，"
                    "也可以只当语气背景；不要做运动报告或健康建议。" % (("（" + detail + "）") if detail else "")
                )
            elif event["kind"] == "recovery_trend":
                purpose = "care"
                summary = (
                    "健康背景：最近几天的恢复状态有一些变化。可以自然问一句最近是不是比较累，"
                    "也可以完全不提；不要报数值、不诊断、不吓唬，也不做医疗建议。"
                )
            else:
                purpose = "chat"
                summary = (
                    "健康背景：最近一周有一些健康记录。可以像熟人一样自然聊聊这周的状态或分享自己的感受，"
                    "也可以只当语气背景；不要做健康周报、不评价表现、不催运动。"
                )
            if preferences.reason(purpose):
                continue
            opened, expires = self._window(event["kind"], occurred)
            if not opened <= at < expires:
                continue
            opportunity = Opportunity(
                "huawei-health-" + hashlib.sha256(
                    ("%s:%s" % (context.key, event["event_id"])).encode("utf-8")).hexdigest()[:24],
                context.scope, opened, expires)
            task_id = self.contacts.task_id(context, opportunity)
            if (self.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id = ?", (task_id,))
                    or self.runtime.read_one("SELECT 1 FROM proactive_contacts WHERE task_id = ?", (task_id,))):
                continue
            values_hash = digest(values)
            record = self._record(context, event, values_hash, opportunity)
            try:
                require(self.contacts.sources.publish(record, observed_at=at))
                require(self.contacts.sources.validate_reference_for_audience(
                    record.reference, recipient=context.actor.subject, observed_at=at))
            except Exception:
                continue
            marker = {
                "event_id": event["event_id"], "kind": event["kind"],
                "settings_hash": values_hash, "source": json.loads(encode(record.reference)),
            }
            thought = Thought(
                "huawei-health-thought-" + digest(marker), opportunity,
                ThoughtKind.CONVERSATION, summary,
                EvidenceSnapshot(context.scope, at, (record.reference,)), 0.75)
            return opportunity, (ContactIntent(purpose, thought),), marker
        return None

    def _validate_source(self, record, at):
        try:
            body = json.loads(record.payload)
            receipt = body.pop("_receipt")
            context = TaskContext.restore(receipt["context"])
            values = self._values(context, at)
            if values is None or digest(values) != receipt["settings_hash"]:
                raise ValueError("health contact settings changed")
            event = next((item for item in self.source.cached_events(
                max_age_seconds=int(MAX_EVENT_AGE.total_seconds()))
                if item.get("event_id") == receipt["event_id"]), None)
            occurred = self._event_time(event.get("occurred_at")) if event else None
            if event is None or occurred is None:
                raise ValueError("health event is no longer current")
            opened, expires = self._window(event["kind"], occurred)
            opportunity = Opportunity(
                "huawei-health-" + hashlib.sha256(
                    ("%s:%s" % (context.key, event["event_id"])).encode("utf-8")).hexdigest()[:24],
                context.scope, opened, expires)
            expected = self._record(context, event, receipt["settings_hash"], opportunity)
            if record.reference != expected.reference or record.content_hash != digest(record.payload):
                raise ValueError("health source changed")
        except TaskError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise TaskError("contact_health_changed", "health contact source is no longer current") from error

    def _record(self, context, event, settings_hash, opportunity):
        body = {
            "event": "huawei_health_completed",
            "kind": event["kind"],
            "occurred_at": event["occurred_at"],
            "facts": event.get("facts") if isinstance(event.get("facts"), dict) else {},
            "meaning": ("这是用户本人授权的华为运动健康已完成记录，不是实时监测、医疗诊断或任务。"
                        "可以自由地自然关心或跳过，不要汇报数据、不催作息、不做健康建议。"),
            "_receipt": {"context": json.loads(encode(context)), "event_id": event["event_id"],
                         "settings_hash": settings_hash},
        }
        payload = encode(body)
        identifier = "contact-huawei-health-source-" + digest([context.key, event["event_id"], settings_hash])
        reference = SourceRef(identifier, SOURCE_OWNER, context.scope, VersionRef(identifier, 1),
                              opportunity.opened_at, opportunity.expires_at)
        return SourceRecord(reference, context.audience, digest(payload), payload)

    def select(self, context, preferences, at):
        values = self._values(context, at)
        if values is None or all(preferences.reason(purpose) for purpose in ("care", "greeting", "chat")):
            return None
        candidate = self._candidate_event(context, values, at, preferences)
        return candidate

    def validate(self, context, marker, at):
        if type(marker) is not dict or set(marker) != {
                "event_id", "kind", "settings_hash", "source"}:
            raise TaskError("contact_health_invalid", "health contact marker is invalid")
        values = self._values(context, at)
        if values is None or digest(values) != marker["settings_hash"]:
            raise TaskError("contact_health_changed", "health contact settings changed")
        try:
            source = source_from_document(marker["source"])
            event = next((item for item in self.source.cached_events(
                max_age_seconds=int(MAX_EVENT_AGE.total_seconds()))
                if item.get("event_id") == marker["event_id"] and item.get("kind") == marker["kind"]), None)
            if source.owner != SOURCE_OWNER or source.scope != context.scope or event is None:
                raise ValueError("health event is no longer current")
            occurred = self._event_time(event.get("occurred_at"))
            if occurred is None:
                raise ValueError("health event has no valid time")
            opened, expires = self._window(event["kind"], occurred)
            opportunity = Opportunity(
                "huawei-health-" + hashlib.sha256(
                    ("%s:%s" % (context.key, event["event_id"])).encode("utf-8")).hexdigest()[:24],
                context.scope, opened, expires)
            record = self._record(context, event, marker["settings_hash"], opportunity)
            if source != record.reference or record.content_hash != digest(record.payload):
                raise ValueError("health source changed")
            require(self.contacts.sources.validate_reference_for_audience(
                source, recipient=context.actor.subject, observed_at=at))
        except TaskError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise TaskError("contact_health_changed", "health contact source is no longer current") from error


__all__ = ["HealthContactEvents", "SOURCE_OWNER"]
