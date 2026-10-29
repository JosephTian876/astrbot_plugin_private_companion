import unittest

from astrbot_host.native_extensions.reality.wakeup_alarm import WakeupAlarmMixin


class _Alarm(WakeupAlarmMixin):
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def mobile_context(self, user_id=""):
        return self._snapshot


def snapshot(minutes=420, age=600, unit="分钟"):
    return {"telemetry": {"available": True, "age_seconds": age,
                          "measurements": [{"type": "sleep_duration", "label": "睡眠时长",
                                            "value": minutes, "unit": unit}]}}


class WakeupSleepHintTests(unittest.TestCase):
    def test_uses_recent_sleep_only_as_optional_background(self):
        alarm = _Alarm(snapshot(420))
        self.assertEqual(alarm._wakeup_user_sleep_hint({"user_id": "u"}),
                         "手机最近上报的睡眠时长约420分钟")
        self.assertEqual(_Alarm(snapshot(7, unit="小时"))._wakeup_user_sleep_hint({"user_id": "u"}),
                         "手机最近上报的睡眠时长约420分钟")
        self.assertEqual(_Alarm(snapshot(420, age=25 * 3600))._wakeup_user_sleep_hint({"user_id": "u"}), "")
        self.assertEqual(_Alarm({"telemetry": {"available": False}})._wakeup_user_sleep_hint({"user_id": "u"}), "")


if __name__ == "__main__":
    unittest.main()
