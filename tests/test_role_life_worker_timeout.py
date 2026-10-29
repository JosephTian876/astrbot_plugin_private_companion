import asyncio
from types import SimpleNamespace
import unittest

from companion_v2.role_work.life.runtime import RoleLifeRuntime


class RoleLifeWorkerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stalled_wake_releases_its_running_key(self):
        app = SimpleNamespace(runtime=None, notify=lambda: None)
        routine = RoleLifeRuntime(app)

        async def stalled_wake():
            await asyncio.Event().wait()

        self.assertTrue(routine.launch("wake:activity", stalled_wake, timeout_seconds=0.01))
        worker = routine._workers["wake:activity"]
        result = await asyncio.wait_for(worker, timeout=0.5)
        await asyncio.sleep(0)

        self.assertEqual(routine.active_handles, ())
        self.assertEqual(result.failure.code, "life_wake_timeout")
        self.assertEqual(routine.results["wake:activity"].failure.code, "life_wake_timeout")


if __name__ == "__main__":
    unittest.main()
