from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from companion_v2.execution.models import TaskContext
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import AudienceRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, ResolvedIdentity, RoleBinding
from companion_v2.proactive.health_events import HealthContactEvents


def context():
    scope = ScopeRef("install-1", "qq", ScopeKind.PRIVATE, "user-1", "bot-1", "account-1", "conversation-1", "session-1")
    user = SubjectRef("install-1", SubjectKind.HUMAN, "user-1")
    role = SubjectRef("install-1", SubjectKind.BOT, "role-1")
    actor = ResolvedIdentity(
        AuthenticatedHostPrincipal("install-1", "host-1", "qq", "user-1", "bot-1", "account-1"),
        user, "binding-1", 1, 1)
    return TaskContext(actor, RoleBinding(scope, role, 1), AudienceRef(scope, (user,)))


class _Sources:
    def __init__(self):
        self.published = []

    def publish(self, record, *, observed_at):
        self.published.append(record)
        return Outcome.success(record.reference)

    def validate_reference_for_audience(self, reference, *, recipient, observed_at):
        return Outcome.success(True)


class _Contacts:
    def __init__(self):
        self.runtime = SimpleNamespace(read_one=lambda *_args, **_kwargs: None)
        self.sources = _Sources()

    @staticmethod
    def task_id(_context, opportunity):
        return "task-" + opportunity.opportunity_id


class _Source:
    def __init__(self, events):
        self.events = events
        self.bound = True

    def is_bound(self):
        return self.bound

    def allowed_for(self, subject_id, role_id):
        return subject_id == "user-1" and role_id == "role-1"

    def cached_events(self, *, max_age_seconds=129600):
        return list(self.events)


class _Preferences:
    values = {"health_enabled": True, "health_proactive_enabled": True}

    @staticmethod
    def reason(purpose):
        return None


class ProactiveHealthEventTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        self.context = context()
        self.contacts = _Contacts()

    def _producer(self, kind="sleep_completed", age_minutes=30, enabled=True):
        occurred = self.at - timedelta(minutes=age_minutes)
        facts = {"activity_type": "跑步", "distance_km": 5.2, "duration_minutes": 32} \
            if kind == "workout_completed" else {"duration_minutes": 430}
        source = _Source([{
            "event_id": "event-" + kind,
            "kind": kind,
            "occurred_at": occurred.isoformat(),
            "summary": "完成记录",
            "facts": facts,
            "age_seconds": age_minutes * 60,
        }])
        settings = (lambda _context, _at: {"health_enabled": enabled,
                                           "health_proactive_enabled": enabled})
        return HealthContactEvents(self.contacts, source, settings)

    def test_recent_sleep_record_becomes_free_form_care_material(self):
        producer = self._producer("sleep_completed", 30)
        candidate = producer.select(self.context, _Preferences(), self.at)
        self.assertIsNotNone(candidate)
        opportunity, intents, marker = candidate
        self.assertEqual(intents[0].purpose, "greeting")
        self.assertIn("不要逐项报", intents[0].thought.summary)
        self.assertEqual(marker["event_id"], "event-sleep_completed")
        producer.validate(self.context, marker, self.at)

    def test_recent_workout_record_keeps_facts_but_not_as_a_report(self):
        producer = self._producer("workout_completed", 10)
        candidate = producer.select(self.context, _Preferences(), self.at)
        self.assertIsNotNone(candidate)
        _, intents, marker = candidate
        self.assertEqual(intents[0].purpose, "care")
        self.assertIn("运动报告", intents[0].thought.summary)
        producer.validate(self.context, marker, self.at)

    def test_weekly_review_uses_existing_chat_purpose(self):
        producer = self._producer("weekly_review", 10)
        candidate = producer.select(self.context, _Preferences(), self.at)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[1][0].purpose, "chat")
        self.assertIn("健康周报", candidate[1][0].thought.summary)

    def test_health_proactive_switch_and_expired_events_do_not_contact(self):
        disabled = self._producer("sleep_completed", 30, enabled=False)
        self.assertIsNone(disabled.select(self.context, _Preferences(), self.at))
        expired = self._producer("workout_completed", 180)
        self.assertIsNone(expired.select(self.context, _Preferences(), self.at))


if __name__ == "__main__":
    unittest.main()
