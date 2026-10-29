"""AstrBot animated-image montage guidance reaches native requests."""

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from tests import astrbot_data_root  # Isolate the SDK before importing the host.

from PIL import Image as PILImage
from astrbot.core.message.components import Image, Reply

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from astrbot_host.core_reply_perception import (
    has_animated_gif_image,
    inject_core_reply_perception_context,
)
from companion_v2.conversation.media.understanding import ASTRBOT_ANIMATED_IMAGE_INSTRUCTION
from tests.animation_support import animation_bytes


class Event:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.extra = {}

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def get_messages(self):
        return self.messages


def payload(request):
    part = next(part for part in request.extra_user_content_parts
                if part.text.startswith("<companion_v2_context>"))
    return json.loads(part.text.removeprefix("<companion_v2_context>\n")
                      .removesuffix("\n</companion_v2_context>"))


class AstrBotAnimatedImageGuidanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="companion-v2-animated-image-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gif = self.root / "animated.gif"
        self.gif.write_bytes(animation_bytes(count=3))
        self.png = self.root / "static.png"
        PILImage.new("RGB", (4, 4), "white").save(self.png)

    def prepared(self, enabled=True):
        return {"astrbot_animated_image_understanding_enabled": enabled}

    async def test_enabled_gif_turn_injects_contact_sheet_rule_once(self):
        event = Event([Image(file=str(self.gif))])
        event.extra[CORE_REPLY_CONTEXT_EXTRA] = {"prepared": self.prepared()}
        request = NS(image_urls=["model-image.jpg"], extra_user_content_parts=[])

        await inject_core_reply_perception_context(event, request)
        await inject_core_reply_perception_context(event, request)

        self.assertEqual(len(request.extra_user_content_parts), 1)
        detail = payload(request)["astrbot_animated_image_understanding"]
        self.assertEqual(detail["state"], "animated_gif_detected")
        self.assertEqual(detail["instruction"], ASTRBOT_ANIMATED_IMAGE_INSTRUCTION)
        self.assertIn("3×3", detail["instruction"])
        self.assertIn("九个人", detail["instruction"])
        self.assertTrue(event.extra[CORE_REPLY_CONTEXT_EXTRA]["_perception_material_injected"])

    async def test_disabled_or_static_image_turns_do_not_inject_the_rule(self):
        for event, request, prepared in (
            (Event([Image(file=str(self.gif))]), NS(image_urls=["image"], extra_user_content_parts=[]),
             self.prepared(False)),
            (Event([Image(file=str(self.png))]), NS(image_urls=["image"], extra_user_content_parts=[]),
             self.prepared(True)),
        ):
            with self.subTest(static=event.messages[0].file == str(self.png), enabled=prepared):
                event.extra[CORE_REPLY_CONTEXT_EXTRA] = {"prepared": prepared}
                await inject_core_reply_perception_context(event, request)
                self.assertEqual(request.extra_user_content_parts, [])

    def test_detection_covers_local_base64_url_and_quoted_gif_components(self):
        direct = Event([Image(file=str(self.gif))])
        self.assertTrue(has_animated_gif_image(direct))
        self.assertFalse(has_animated_gif_image(Event([Image(file=str(self.png))])))

        encoded = base64.b64encode(animation_bytes(count=2)).decode("ascii")
        self.assertTrue(has_animated_gif_image(Event([Image(file="base64://" + encoded)])))
        self.assertTrue(has_animated_gif_image(Event([
            Image(file="https://example.invalid/sticker.gif?token=1")])))
        self.assertTrue(has_animated_gif_image(Event([
            Reply(id="1", chain=[Image(file=str(self.gif))])])))


if __name__ == "__main__":
    unittest.main()