from datetime import datetime, timedelta, timezone
import unittest

from companion_v2.proactive.sleep_context import SleepContext, from_health_source


NOW = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)


def event(duration=480, ended=None, identifier="sleep-1"):
    return {"kind": "sleep_completed", "event_id": identifier,
            "occurred_at": (ended or NOW - timedelta(minutes=10)).isoformat(),
            "facts": {"duration_minutes": duration}}


class SleepContextTests(unittest.TestCase):
    def test_projects_wake_relative_window_and_qualitative_hint(self):
        sleep = SleepContext.from_events([event(300)], NOW)
        self.assertIsNotNone(sleep)
        self.assertEqual(sleep.qualifier, "最近一次睡眠偏短")
        self.assertEqual(sleep.morning_opened_at, NOW + timedelta(minutes=10))
        self.assertIn("不报具体数值", sleep.morning_hint())
        self.assertEqual(sleep.quiet_until, NOW + timedelta(minutes=10))

    def test_ignores_old_missing_and_future_records(self):
        self.assertIsNone(SleepContext.from_events([event(480, NOW - timedelta(hours=48))], NOW))
        self.assertIsNone(SleepContext.from_events([event(480, NOW + timedelta(hours=1))], NOW))
        self.assertIsNone(SleepContext.from_events([{"kind": "sleep_completed", "facts": {}}], NOW))
        self.assertIsNone(SleepContext.from_events([], NOW))

    def test_prefers_latest_completed_sleep(self):
        sleep = SleepContext.from_events([
            event(300, NOW - timedelta(hours=3), "old"),
            event(480, NOW - timedelta(minutes=5), "new"),
        ], NOW)
        self.assertEqual(sleep.event_id, "new")
        self.assertEqual(sleep.qualifier, "最近一次睡眠时长正常")

    def test_health_source_adapter_is_failure_tolerant(self):
        class Source:
            def cached_events(self, *, max_age_seconds):
                return [event(600, NOW - timedelta(minutes=1))]
        self.assertEqual(from_health_source(Source(), NOW).qualifier, "最近一次睡眠偏长")
        self.assertIsNone(from_health_source(None, NOW))
        class Broken:
            def cached_events(self, *, max_age_seconds):
                raise RuntimeError("unavailable")
        self.assertIsNone(from_health_source(Broken(), NOW))


if __name__ == "__main__":
    unittest.main()
