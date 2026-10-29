from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from astrbot_host.core_reply_context import build_core_reply_prompt
from astrbot_host.core_reply_health import health_perception_snapshot
from companion_v2.conversation.health_context import (
    HEALTH_INSTRUCTION, MAX_MEASUREMENTS, project_health_context,
)
from companion_v2.foundation.refs import ScopeKind


AT = datetime(2026, 10, 1, 9, 50, 1, tzinfo=timezone.utc)


def _mobile_snapshot():
    return {
        "available": True,
        "telemetry": {
            "available": True,
            "captured_at": 1790848200,
            "age_seconds": 1,
            "measurements": [
                {"type": "heart_rate", "label": "心率", "value": 62, "unit": "bpm",
                 "captured_at": 1790848200},
                {"type": "steps", "label": "步数", "value": 3200, "unit": "步",
                 "captured_at": 1790848200},
                {"type": "broken", "label": "坏数据", "value": float("nan"), "unit": "x"},
            ],
            "activity": {"label": "步行"},
        },
    }


def _messages():
    return NS(
        role_profile_context=None,
        acquaintance=NS(card_contribution=lambda *_args, **_kwargs: None),
        knowledge=None,
        proactive_inspirations=None,
        relationship_profile_context=None,
        memory_context=None,
        relationship_context=None,
        life_context=None,
        role_growth_context=None,
        role_self_review_context=None,
        open_items_context=None,
        role_state_context=None,
        expression_style_context=None,
        group_context=NS(authorize=lambda *_args: None),
    )


class HealthProjectionTests(unittest.TestCase):
    def test_projection_is_bounded_private_and_drops_invalid_readings(self):
        result = project_health_context(_mobile_snapshot(), observed_at=AT)
        self.assertEqual(result["privacy"], "private_only")
        self.assertEqual([item["type"] for item in result["measurements"]],
                         ["heart_rate", "steps"])
        self.assertEqual(result["activity"], "步行")
        self.assertIn("心率 62 bpm", result["summary"])
        self.assertNotIn("坏数据", result["summary"])

    def test_projection_deduplicates_and_caps_measurements(self):
        snapshot = _mobile_snapshot()
        snapshot["telemetry"]["measurements"] = [
            {"type": "heart_rate", "value": index, "unit": "bpm"} for index in range(MAX_MEASUREMENTS + 3)
        ]
        result = project_health_context(snapshot, observed_at=AT)
        self.assertEqual(len(result["measurements"]), 1)
        self.assertEqual(result["measurements"][0]["value"], 0)

    def test_projection_rejects_unavailable_or_empty_telemetry(self):
        self.assertIsNone(project_health_context({"telemetry": {"available": False}}, observed_at=AT))
        self.assertIsNone(project_health_context({"telemetry": {"available": True, "measurements": []}},
                                                 observed_at=AT))
        self.assertIsNone(project_health_context({}, observed_at=AT))


class HealthPromptTests(unittest.TestCase):
    def _application(self, settings):
        return NS(messages=_messages(),
                  perception_settings=NS(capture=lambda *_args: {"settings": settings}))

    def test_health_context_is_injected_only_in_an_enabled_private_window(self):
        settings = {
            "guidance": "", "calendar_timezone": "Asia/Shanghai", "calendar_enabled": False,
            "health_enabled": True, "platform_enabled": True, "model_route_enabled": True,
            "configured_modalities_enabled": True,
        }
        projection = {"version": "conversation.health.v1", "privacy": "private_only",
                      "measurements": [{"type": "steps", "value": 3200, "unit": "步"}],
                      "summary": "步数 3200 步"}
        context = NS(scope=NS(kind=ScopeKind.PRIVATE), key="health-private",
                     actor=NS(), role_binding=NS(role=NS()))
        with patch("astrbot_host.core_reply_context.health_perception_snapshot",
                   return_value=projection) as health:
            prompt = build_core_reply_prompt(
                self._application(settings), context, NS(text="今天走了多少", event_id="health"),
                at=AT)
        perception = json.loads(prompt.perception_context)
        self.assertEqual(perception["health_context"], projection)
        self.assertIn(HEALTH_INSTRUCTION, prompt.perception_instructions)
        self.assertEqual(health.call_count, 1)

        context.scope.kind = ScopeKind.GROUP
        with patch("astrbot_host.core_reply_context.health_perception_snapshot",
                   return_value=projection) as health:
            prompt = build_core_reply_prompt(
                self._application(settings), context, NS(text="今天走了多少", event_id="health-group"),
                at=AT)
        self.assertNotIn("health_context", json.loads(prompt.perception_context))
        self.assertFalse(health.called)

    def test_huawei_health_material_is_used_without_mobile_provider(self):
        settings = {
            "guidance": "", "calendar_timezone": "Asia/Shanghai", "calendar_enabled": False,
            "health_enabled": True, "platform_enabled": True, "model_route_enabled": True,
            "configured_modalities_enabled": True,
        }
        material = {"basis": "huawei_health_cloud", "privacy": "private_only",
                    "observed_at": "2026-10-01T09:50:00+00:00", "age_seconds": 12,
                    "summary": "最近睡眠：10-01 总睡 420 分，得分 82"}
        context = NS(scope=NS(kind=ScopeKind.PRIVATE), key="huawei-health",
                     actor=NS(), role_binding=NS(role=NS()))
        with patch("astrbot_host.core_reply_context.health_perception_snapshot") as mobile:
            prompt = build_core_reply_prompt(
                self._application(settings), context, NS(text="昨晚睡得怎么样", event_id="hw"),
                at=AT, health_material=material)
        self.assertEqual(json.loads(prompt.perception_context)["health_context"], material)
        self.assertIn(HEALTH_INSTRUCTION, prompt.perception_instructions)
        self.assertFalse(mobile.called)

    def test_empty_health_source_does_not_add_instruction(self):
        settings = {
            "guidance": "", "calendar_timezone": "Asia/Shanghai", "calendar_enabled": False,
            "health_enabled": True, "platform_enabled": True, "model_route_enabled": True,
            "configured_modalities_enabled": True,
        }
        context = NS(scope=NS(kind=ScopeKind.PRIVATE), key="health-empty",
                     actor=NS(), role_binding=NS(role=NS()))
        with patch("astrbot_host.core_reply_context.health_perception_snapshot", return_value=None):
            prompt = build_core_reply_prompt(self._application(settings), context,
                                             NS(text="你看到了什么", event_id="health-empty"), at=AT)
        self.assertNotIn("health_context", json.loads(prompt.perception_context))
        self.assertNotIn(HEALTH_INSTRUCTION, prompt.perception_instructions)

    def test_health_switch_off_skips_source(self):
        settings = {
            "guidance": "", "calendar_timezone": "Asia/Shanghai", "calendar_enabled": False,
            "health_enabled": False, "platform_enabled": True, "model_route_enabled": True,
            "configured_modalities_enabled": True,
        }
        context = NS(scope=NS(kind=ScopeKind.PRIVATE), key="health-off",
                     actor=NS(), role_binding=NS(role=NS()))
        with patch("astrbot_host.core_reply_context.health_perception_snapshot") as health:
            build_core_reply_prompt(self._application(settings), context,
                                    NS(text="你看到了什么", event_id="health-off"), at=AT)
        self.assertFalse(health.called)


class HealthHostAdapterTests(unittest.TestCase):
    def test_adapter_requires_private_target_role_and_consent(self):
        service = Mock()
        service.mobile_context.return_value = _mobile_snapshot()
        registry = Mock()
        registry.active_service.return_value = service
        app = NS(runtime=object(), native_tool_port=registry)
        actor = NS(principal=NS(native_account_id="user-1"))
        context = NS(scope=NS(kind=ScopeKind.PRIVATE), actor=actor,
                     role_binding=NS(role=NS()))
        with patch("astrbot_host.core_reply_health._role_enabled", return_value=True), \
             patch("astrbot_host.core_reply_health._consent_active", return_value=True):
            result = health_perception_snapshot(app, context, AT)
        self.assertEqual(result["measurements"][0]["label"], "心率")
        service.mobile_context.assert_called_once_with("user-1")

        context.scope.kind = ScopeKind.GROUP
        self.assertIsNone(health_perception_snapshot(app, context, AT))


if __name__ == "__main__":
    unittest.main()
