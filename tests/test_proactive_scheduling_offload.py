"""The contact planner is off the AstrBot event loop, but task activation stays on it."""

import asyncio
import threading
import unittest
from types import SimpleNamespace

from companion_v2.proactive.scheduling import ContactSchedule


class ContactScheduleOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_advance_async_plans_in_worker_and_activates_on_loop(self):
        scheduler = object.__new__(ContactSchedule)
        scheduler._closed = False
        scheduler._advancing = False
        scheduler._workers = {}
        scheduler.tasks = SimpleNamespace(now=lambda: object())
        scheduler._cursor = 1
        scheduler._native_cursor = 1
        row = {"origin_key": "origin", "sequence": 1}
        plan = ("plan",)

        seen = {}
        loop_thread = threading.get_ident()
        scheduler._begin_tick = lambda: (object(), (row,), 1, 1, 0)
        scheduler._row_cursor = lambda current: seen.setdefault("cursor", current)

        def plan_one(current, now):
            seen["plan_thread"] = threading.get_ident()
            threading.Event().wait(0.05)
            return plan

        def activate(current, current_plan, available, now):
            seen["activate_thread"] = threading.get_ident()
            seen["plan"] = current_plan
            return available - 1

        scheduler._plan_one = plan_one
        scheduler._activate_plan = activate
        scheduler._note_coordinator = lambda *args: seen.setdefault("coordinator_thread", threading.get_ident())

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(3):
                await asyncio.sleep(0.005)
                ticks += 1

        await asyncio.gather(ticker(), scheduler.advance_async())
        self.assertNotEqual(seen["plan_thread"], loop_thread)
        self.assertEqual(seen["activate_thread"], loop_thread)
        self.assertEqual(seen["coordinator_thread"], loop_thread)
        self.assertEqual(seen["plan"], plan)
        self.assertGreater(ticks, 0, "event loop must keep running while candidate sources are evaluated")

    async def test_close_waits_for_inflight_planner_before_closing_sources(self):
        scheduler = object.__new__(ContactSchedule)
        scheduler._closed = False
        scheduler._workers = {}
        scheduler._judges = {}
        for name in ("events", "growth_events", "journal_events", "calendar_events",
                     "reminiscence_events", "weather_events", "weather_change_events",
                     "official_alert_events"):
            setattr(scheduler, name, None)
        seen = {}
        planning = asyncio.create_task(asyncio.sleep(0.05))
        scheduler._planning = planning

        class Source:
            def close(self):
                seen["closed_after_planner"] = planning.done()

        scheduler.balance_events = Source()
        scheduler.news_events = Source()
        scheduler.screen_events = Source()

        await scheduler.close()
        self.assertTrue(seen["closed_after_planner"], "planner must finish before sources close")
        self.assertTrue(planning.done())


if __name__ == "__main__":
    unittest.main()
