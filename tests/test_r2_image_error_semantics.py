"""Round-2 F-06: image 4xx semantics, redacted diagnostics and terminal states.

The suite is offline: ``aiohttp.ClientSession`` / ``FormData`` are replaced by
recording fakes, every store is a temporary SQLite file, and no prompt, key or
reference byte ever leaves the process.

Two kinds of tests live here on purpose:

* **new oracle** tests fail on the pre-fix code (they assert the corrected
  semantics: definitive 4xx rejections are ``failed``, not permanent
  ``unknown``; the body is read once and redacted; account refusals stop, while
  endpoint-scoped 4xx may switch — the round-3 suite
  ``test_r3_image_failure_matrix.py`` owns the full scoped matrix);
* **invariant** tests pass before and after the fix and exist to stop the fix
  from turning a possibly-billed ``unknown`` into a retry.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from astrbot_host.native_extensions.image_native import output_routing
from astrbot_host.native_extensions.image_native.adapter import NativeImageProvider
from astrbot_host.native_extensions.image_native.extension import ImageExtensionAPI
from astrbot_host.native_extensions.image_native.owner import NativeImageOwner
from astrbot_host.native_extensions.image_native.store import ImageStore
from astrbot_host.native_extensions.image_native.types import (
    ImageInvocation,
    NativeImageError,
    ReferenceImage,
)
from companion_v2.capabilities import native as native_contract
from companion_v2.execution.models import SideEffect
from companion_v2.product import PRODUCT_VERSION

PNG_BYTES = b"\x89PNG\r\n\x1a\n-round-2-image-body"
API_KEY = "sk-round2-primary-secret"
WORKSPACE_SECRET = "workspace-round2-secret"
CUSTOM_HEADER_PROMPT = "A very secret prompt about a blue cup."


# --------------------------------------------------------------------------- #
# Recording aiohttp stand-ins
# --------------------------------------------------------------------------- #

class _CountingContent:
    """Fake ``response.content`` that records how many bytes were consumed."""

    def __init__(self, payload: bytes, step: int = 1024):
        self.payload = payload
        self.step = step
        self.consumed = 0
        self.read_limit = None

    async def iter_chunked(self, size):
        limit = min(size, self.step)
        for index in range(0, len(self.payload), limit):
            block = self.payload[index:index + limit]
            self.consumed += len(block)
            yield block

    async def read(self, n: int = -1):
        self.read_limit = n
        block = self.payload if n is None or n < 0 else self.payload[:n]
        self.consumed += len(block)
        return block


class FakeResponse:
    def __init__(self, payload: bytes = b"", status: int = 200, headers=None):
        self.status = status
        self.content = _CountingContent(payload)
        self.headers = dict(headers or {})


class _RequestCall:
    def __init__(self, make):
        self.make = make

    async def __aenter__(self):
        value = self.make()
        if isinstance(value, BaseException):
            raise value
        return value

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, http):
        self.http = http

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def post(self, url, **kwargs):
        return _RequestCall(lambda: self.http.respond("POST", url, kwargs))

    def get(self, url, **kwargs):
        return _RequestCall(lambda: self.http.respond("GET", url, kwargs))


class FakeHTTP:
    """Canned per-URL replies plus an ordered record of every call."""

    def __init__(self):
        self.calls = []
        self.sessions = []
        self.replies = {}

    def reply(self, url: str, value):
        self.replies[url] = value

    def session(self, *, timeout=None, trust_env=None):
        self.sessions.append({"timeout": timeout, "trust_env": trust_env})
        return FakeSession(self)

    def respond(self, method: str, url: str, kwargs: dict):
        self.calls.append({"method": method.upper(), "url": url, "kwargs": kwargs})
        value = self.replies.get(url)
        if value is None:
            raise AssertionError(f"unexpected {method} {url}: {sorted(self.replies)}")
        value = value(url, kwargs) if callable(value) else value
        if isinstance(value, BaseException):
            raise value
        return value

    def posts(self, name: str | None = None) -> list:
        calls = [call for call in self.calls if call["method"] == "POST"]
        return [call for call in calls if name is None or name + ".example.test" in call["url"]]


class _RecordingForm:
    def __init__(self):
        self.fields = []

    def add_field(self, name, value, *, filename=None, content_type=None):
        self.fields.append({"name": name, "value": value, "filename": filename,
                            "content_type": content_type})


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

def endpoint(name: str = "primary", **overrides) -> dict:
    value = {
        "id": name, "name": name.title(), "enabled": True, "platform": "openai",
        "model": "gpt-image-1", "width": 1024, "height": 1024,
        "base_url": f"https://{name}.example.test/v1", "api_key": API_KEY,
        "custom_headers": {"X-Workspace": WORKSPACE_SECRET},
        "timeout_seconds": 60, "quality": "", "response_format": "",
        "max_reference_images": 1, "size_label": "1024x1024",
    }
    value.update(overrides)
    return value


def generate_url(name: str = "primary") -> str:
    return f"https://{name}.example.test/v1/images/generations"


def route_settings(endpoints, routes=None, **overrides) -> dict:
    value = {"enabled": True, "backend": "online_api", "endpoints": list(endpoints),
             "network": {}, "sd_webui": {}, "comfyui": {}}
    if routes is not None:
        value["operation_routes"] = routes
    value.update(overrides)
    return value


def invocation(operation: str = "generate", prompt: str = CUSTOM_HEADER_PROMPT) -> ImageInvocation:
    return ImageInvocation("scope-1", "request-1", "invocation-1", "origin-1", operation, prompt)


def reference(name: str = "ref-1") -> ReferenceImage:
    return ReferenceImage(name, b"synthetic-reference", "source-1", "0" * 64)


def openai_error(code="invalid_value", message="size must be one of 1024x1024",
                 param="size", error_type="invalid_request_error") -> bytes:
    return json.dumps({"error": {"message": message, "type": error_type,
                                 "code": code, "param": param}}).encode("utf-8")


def b64_success() -> FakeResponse:
    payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]})
    return FakeResponse(payload.encode("utf-8"))


class FakeHttpCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = FakeHTTP()
        patches = [mock.patch.object(output_routing.aiohttp, "ClientSession", self.http.session),
                   mock.patch.object(output_routing.aiohttp, "FormData", _RecordingForm)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def run_online(self, config, *, operation="generate", references=(), guard=None):
        backend = output_routing.OnlineImageBackend(config)
        return await backend.execute(invocation(operation), tuple(references), guard or (lambda: None))

    async def routing_error(self, settings, *, operation="generate", references=(), guard=None):
        backend = output_routing.OutputRoutingBackend(settings, Path(self.temporary.name))
        with self.assertRaises(NativeImageError) as caught:
            await backend.execute(invocation(operation), tuple(references), guard or (lambda: None))
        return caught.exception

    def setUpTemporary(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)


# --------------------------------------------------------------------------- #
# 1. Per-endpoint classification (OnlineImageBackend)
# --------------------------------------------------------------------------- #

class EndpointClassificationTests(FakeHttpCase):
    async def test_account_statuses_are_definitive_and_never_switchable(self):
        # Round 3 moved endpoint-scoped 4xx (400 request shape, 403 entitlement,
        # 413 payload, 422 schema) to the switchable set; 401/402 stay global.
        for status in (401, 402):
            with self.subTest(status=status):
                self.http.calls.clear()
                self.http.reply(generate_url(), FakeResponse(openai_error(), status=status))
                with self.assertRaises(NativeImageError) as caught:
                    await self.run_online(endpoint())
                error = caught.exception
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertFalse(error.switchable)
                self.assertFalse(error.unconfirmed)
                self.assertEqual(error.code, "native_endpoint_rejected")
                self.assertEqual(error.diagnostics[0]["scope"], "account")
                self.assertEqual(len(error.diagnostics), 1)
                record = error.diagnostics[0]
                self.assertEqual(record["status"], status)
                self.assertEqual(record["endpoint_id"], "primary")
                self.assertTrue(record["detail"])

    async def test_switch_statuses_are_definitive_but_keep_going(self):
        for status in (404, 405):
            with self.subTest(status=status):
                self.http.calls.clear()
                self.http.reply(generate_url(), FakeResponse(openai_error(), status=status))
                with self.assertRaises(NativeImageError) as caught:
                    await self.run_online(endpoint())
                error = caught.exception
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertTrue(error.switchable)
                self.assertFalse(error.unconfirmed)
                self.assertEqual(error.code, "native_endpoint_rejected")
                self.assertEqual(error.diagnostics[0]["status"], status)

    async def test_rate_limit_is_a_switchable_definitive_rejection(self):
        self.http.reply(generate_url(), FakeResponse(openai_error(), status=429))

        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint())

        error = caught.exception
        self.assertIsInstance(error, output_routing.Rejected)
        self.assertTrue(error.switchable)
        self.assertFalse(error.unconfirmed)
        self.assertEqual(error.code, "backend_rate_limited")
        self.assertEqual(error.diagnostics[0]["reason"], "rate_limited")

    async def test_indeterminate_statuses_stay_unconfirmed(self):
        for status in (408, 409, 418, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.http.calls.clear()
                self.http.reply(generate_url(), FakeResponse(openai_error(), status=status))
                with self.assertRaises(NativeImageError) as caught:
                    await self.run_online(endpoint())
                error = caught.exception
                self.assertNotIsInstance(error, output_routing.Rejected)
                self.assertTrue(error.unconfirmed)
                self.assertEqual(error.code, "backend_result_unconfirmed")
                self.assertEqual(error.diagnostics[0]["status"], status)

    def test_status_tables_document_the_switch_matrix(self):
        stop = getattr(output_routing, "STOP_FAILURE_STATUSES", frozenset())
        switch = getattr(output_routing, "SWITCH_FAILURE_STATUSES", frozenset())
        rate = getattr(output_routing, "RATE_LIMIT_STATUSES", frozenset())
        definitive = getattr(output_routing, "DEFINITIVE_FAILURE_STATUSES", frozenset())
        self.assertEqual(set(stop), {401, 402})
        self.assertEqual(set(switch), {400, 403, 404, 405, 413, 422})
        self.assertEqual(set(rate), {429})
        self.assertEqual(set(definitive), set(stop) | set(switch) | set(rate))
        self.assertEqual(set(stop) & set(switch), set())
        self.assertEqual(set(stop) & set(rate), set())

    async def test_error_body_is_read_once_and_bounded(self):
        body = b'{"error":{"message":"' + b"x" * (1024 * 1024) + b'"}}'
        response = FakeResponse(body, status=422)
        self.http.reply(generate_url(), response)

        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint())

        record = caught.exception.diagnostics[0]
        self.assertEqual(getattr(output_routing, "MAX_ERROR_BODY_BYTES", 4096), 4096)
        self.assertLessEqual(response.content.consumed, 4096)
        self.assertLessEqual(len(record["detail"]), 160)
        self.assertGreater(response.content.consumed, 0)

    async def test_openai_error_fields_are_kept_and_secrets_are_removed(self):
        body = json.dumps({"error": {
            "message": f"Incorrect API key provided: {API_KEY}. Header X-Workspace="
                       f"{WORKSPACE_SECRET}. Prompt was {CUSTOM_HEADER_PROMPT}",
            "type": "invalid_request_error", "code": "invalid_value", "param": "size"}}).encode()
        self.http.reply(generate_url(), FakeResponse(body, status=422))

        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint())

        detail = caught.exception.diagnostics[0]["detail"]
        self.assertIn("code=invalid_value", detail)
        self.assertIn("param=size", detail)
        self.assertIn("type=invalid_request_error", detail)
        self.assertNotIn(API_KEY, detail)
        self.assertNotIn(WORKSPACE_SECRET, detail)
        self.assertNotIn(CUSTOM_HEADER_PROMPT, detail)
        self.assertNotIn("Bearer", detail)
        self.assertIn("***", detail)

    async def test_minimax_base_resp_and_plain_text_bodies_are_understood(self):
        minimax_url = "https://primary.example.test/v1/image_generation"
        base_resp = json.dumps({"base_resp": {"status_code": 1002, "status_msg": "rate limit"}}).encode()
        self.http.reply(minimax_url, FakeResponse(base_resp, status=403))
        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint(platform="minimax", model="image-01",
                                            base_url="https://primary.example.test/v1"))
        detail = caught.exception.diagnostics[0]["detail"]
        self.assertIn("code=1002", detail)
        self.assertIn("rate limit", detail)

        self.http.calls.clear()
        self.http.reply(minimax_url, FakeResponse(b"upstream said no", status=502))
        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint(platform="minimax", model="image-01",
                                            base_url="https://primary.example.test/v1"))
        self.assertEqual(caught.exception.diagnostics[0]["detail"], "upstream said no")

    async def test_control_characters_and_binary_bodies_are_flattened(self):
        body = b'{"error":{"message":"line one\\nline two\\t\\u0000tab"}}'
        self.http.reply(generate_url(), FakeResponse(body, status=400))
        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint())
        detail = caught.exception.diagnostics[0]["detail"]
        self.assertNotIn("\n", detail)
        self.assertNotIn("\t", detail)
        self.assertNotIn("\x00", detail)
        self.assertIn("line one", detail)
        self.assertIn("line two", detail)

        self.http.calls.clear()
        self.http.reply(generate_url(), FakeResponse(b"\x00\x01\x02\xff" * 100, status=500))
        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint())
        self.assertTrue(caught.exception.unconfirmed)
        self.assertLessEqual(len(caught.exception.diagnostics[0]["detail"]), 160)

    async def test_retry_after_is_reported_but_never_waited_on(self):
        self.http.reply(generate_url(), FakeResponse(openai_error(), status=429,
                                                     headers={"Retry-After": "30"}))
        started = asyncio.get_running_loop().time()

        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint())

        self.assertLess(asyncio.get_running_loop().time() - started, 2)
        self.assertEqual(caught.exception.diagnostics[0]["retry_after"], "30")

    async def test_success_receipt_is_unchanged_and_carries_no_diagnostics(self):
        self.http.reply(generate_url(), b64_success())

        result = await self.run_online(endpoint())

        self.assertEqual(result.images, (PNG_BYTES,))
        self.assertEqual(set(result.receipt), {"backend", "endpoint_id", "model", "response_received"})
        self.assertNotIn("diagnostics", json.dumps(result.receipt))

    async def test_local_reference_limit_never_sends_a_request(self):
        with self.assertRaises(NativeImageError) as caught:
            await self.run_online(endpoint(max_reference_images=1),
                                  operation="edit", references=[reference("a"), reference("b")])

        error = caught.exception
        self.assertIsInstance(error, output_routing.Rejected)
        self.assertFalse(error.unconfirmed)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(error.diagnostics[0]["endpoint_id"], "primary")
        self.assertIn("max=1", error.diagnostics[0]["detail"])
        self.assertIn("provided=2", error.diagnostics[0]["detail"])


# --------------------------------------------------------------------------- #
# 2. Routing matrix (OutputRoutingBackend)
# --------------------------------------------------------------------------- #

class RoutingMatrixTests(FakeHttpCase):
    def setUp(self):
        super().setUp()
        self.setUpTemporary()
        self.primary, self.secondary, self.tertiary = (
            endpoint("primary"), endpoint("secondary"), endpoint("tertiary"))

    def settings(self, *names, routes=None):
        by_name = {"primary": self.primary, "secondary": self.secondary, "tertiary": self.tertiary}
        picked = [by_name[name] for name in names]
        ordered = {operation: [f"online:{name}" for name in names] for operation in ("generate", "edit")}
        return route_settings(picked, routes if routes is not None else ordered)

    async def test_stop_status_does_not_touch_the_second_endpoint(self):
        self.http.reply(generate_url("primary"), FakeResponse(openai_error(), status=401))
        self.http.reply(generate_url("secondary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "native_endpoint_rejected")
        self.assertFalse(error.unconfirmed)
        self.assertEqual(len(self.http.posts()), 1)
        self.assertEqual(self.http.posts()[0]["url"], generate_url("primary"))
        self.assertEqual(len(error.diagnostics), 1)
        self.assertEqual(error.diagnostics[0]["status"], 401)

    async def test_not_found_switches_to_the_next_endpoint(self):
        self.http.reply(generate_url("primary"), FakeResponse(openai_error(), status=404))
        self.http.reply(generate_url("secondary"), b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(), (), lambda: None)

        self.assertEqual(result.images, (PNG_BYTES,))
        self.assertEqual(result.receipt["endpoint_id"], "secondary")
        self.assertEqual(len(self.http.posts()), 2)

    async def test_all_switchable_failures_report_endpoints_rejected(self):
        self.http.reply(generate_url("primary"), FakeResponse(openai_error(), status=404))
        self.http.reply(generate_url("secondary"), FakeResponse(openai_error(), status=405))

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "native_endpoints_rejected")
        self.assertFalse(error.unconfirmed)
        self.assertEqual([item["status"] for item in error.diagnostics], [404, 405])
        self.assertEqual(len(self.http.posts()), 2)

    async def test_rate_limit_spends_one_alternative_then_stops(self):
        for name in ("primary", "secondary", "tertiary"):
            self.http.reply(generate_url(name),
                            FakeResponse(openai_error(message="slow down"), status=429))

        error = await self.routing_error(self.settings("primary", "secondary", "tertiary"))

        self.assertEqual(error.code, "backend_rate_limited")
        self.assertFalse(error.unconfirmed)
        self.assertEqual(len(self.http.posts()), 2)
        self.assertEqual([item["endpoint_id"] for item in error.diagnostics], ["primary", "secondary"])

    async def test_rate_limit_then_success(self):
        self.http.reply(generate_url("primary"), FakeResponse(openai_error(), status=429))
        self.http.reply(generate_url("secondary"), b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(), (), lambda: None)

        self.assertEqual(result.receipt["endpoint_id"], "secondary")
        self.assertEqual(len(self.http.posts()), 2)

    async def test_server_error_is_never_retried_on_another_endpoint(self):
        self.http.reply(generate_url("primary"), FakeResponse(openai_error(), status=500))
        self.http.reply(generate_url("secondary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "backend_result_unconfirmed")
        self.assertTrue(error.unconfirmed)
        self.assertEqual(len(self.http.posts()), 1)

    async def test_client_timeout_is_unknown_and_not_retried(self):
        self.http.reply(generate_url("primary"), asyncio.TimeoutError())
        self.http.reply(generate_url("secondary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "backend_result_unconfirmed")
        self.assertTrue(error.unconfirmed)
        self.assertEqual(len(self.http.posts()), 1)

    async def test_no_route_keeps_the_unconfigured_code(self):
        error = await self.routing_error(route_settings([], {"generate": [], "edit": []}))

        self.assertEqual(error.code, "native_backend_unconfigured")
        self.assertFalse(error.unconfirmed)
        self.assertEqual(self.http.posts(), [])

    async def test_route_without_the_operation_is_not_unconfigured(self):
        minimax = endpoint("primary", platform="minimax", model="image-01")

        error = await self.routing_error(route_settings([minimax], {"generate": ["online:primary"],
                                                                   "edit": ["online:primary"]}),
                                         operation="edit")

        self.assertEqual(error.code, "unsupported_image_operation")
        self.assertFalse(error.unconfirmed)
        self.assertEqual(self.http.posts(), [])

    async def test_authority_revocation_before_dispatch_is_never_swallowed(self):
        async def guard():
            raise NativeImageError("image_authority_revoked")

        error = await self.routing_error(self.settings("primary", "secondary"), guard=guard)

        self.assertEqual(error.code, "image_authority_revoked")
        self.assertEqual(self.http.posts(), [])

    async def test_local_reference_limit_uses_the_next_endpoint_without_a_post(self):
        self.primary["max_reference_images"] = 1
        self.secondary["max_reference_images"] = 4
        self.http.reply("https://secondary.example.test/v1/images/edits", b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(operation="edit"),
                                      (reference("a"), reference("b")), lambda: None)

        self.assertEqual(result.images, (PNG_BYTES,))
        self.assertEqual([call["url"] for call in self.http.posts()],
                         ["https://secondary.example.test/v1/images/edits"])

    async def test_local_reference_limit_on_every_endpoint_reports_request_invalid(self):
        self.primary["max_reference_images"] = 1
        self.secondary["max_reference_images"] = 1

        error = await self.routing_error(self.settings("primary", "secondary"),
                                         operation="edit",
                                         references=(reference("a"), reference("b")))

        self.assertEqual(error.code, "backend_request_invalid")
        self.assertFalse(error.unconfirmed)
        self.assertEqual(self.http.posts(), [])
        self.assertEqual([item["endpoint_id"] for item in error.diagnostics],
                         ["primary", "secondary"])

    async def test_routing_diagnostics_never_leak_the_prompt_or_key(self):
        body = json.dumps({"error": {"message": f"bad {API_KEY} for {CUSTOM_HEADER_PROMPT}"}}).encode()
        self.http.reply(generate_url("primary"), FakeResponse(body, status=400))
        self.http.reply(generate_url("secondary"), FakeResponse(body, status=400))

        error = await self.routing_error(self.settings("primary", "secondary"))

        serialized = json.dumps(error.diagnostics)
        self.assertNotIn(API_KEY, serialized)
        self.assertNotIn(CUSTOM_HEADER_PROMPT, serialized)
        # A 400 whose detail does not name a global cause is endpoint-scoped
        # since round 3, so both endpoints are tried and both refused.
        self.assertEqual(error.code, "native_endpoints_rejected")
        self.assertEqual([item["endpoint_id"] for item in error.diagnostics],
                         ["primary", "secondary"])
        self.assertEqual([item["scope"] for item in error.diagnostics],
                         ["endpoint", "endpoint"])


# --------------------------------------------------------------------------- #
# 3. Owner / store state machine
# --------------------------------------------------------------------------- #

class StubBackend:
    """Calls the dispatch fence, then fails like a real backend would."""

    revision = "stub-revision"
    operations = ("generate", "edit")

    def __init__(self, error, *, after_dispatch=True):
        self.error = error
        self.after_dispatch = after_dispatch
        self.calls = 0

    async def execute(self, invocation, references, before_dispatch):
        self.calls += 1
        if self.after_dispatch:
            await before_dispatch()
        raise self.error


class OwnerStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def owner(self, backend):
        return NativeImageOwner(self.root, backend)

    async def test_definitive_rejection_is_recorded_as_failed(self):
        rejection = output_routing.Rejected("native_endpoint_rejected", endpoint_id="primary",
                                            status=401, reason="unauthorized",
                                            detail="code=invalid_api_key", switchable=False)
        owner = self.owner(StubBackend(rejection))

        operation = await owner.execute(invocation(), before_dispatch=lambda: None)

        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error"], "native_endpoint_rejected")
        self.assertEqual(operation["receipt"]["diagnostics"][0]["status"], 401)

    async def test_unconfirmed_failure_is_recorded_as_unknown(self):
        error = NativeImageError("backend_result_unconfirmed",
                                 diagnostics=({"endpoint_id": "primary", "status": 500,
                                               "reason": "server_error", "detail": "boom"},))
        owner = self.owner(StubBackend(error))

        operation = await owner.execute(invocation(), before_dispatch=lambda: None)

        self.assertEqual(operation["state"], "unknown")
        self.assertEqual(operation["error"], "backend_result_unconfirmed")
        self.assertEqual(operation["receipt"]["diagnostics"][0]["status"], 500)

    async def test_failure_before_dispatch_is_failed_even_when_unconfirmed(self):
        owner = self.owner(StubBackend(NativeImageError("backend_result_unconfirmed"),
                                       after_dispatch=False))

        operation = await owner.execute(invocation(), before_dispatch=lambda: None)

        self.assertEqual(operation["state"], "failed")

    async def test_unknown_operation_is_replayed_without_a_second_dispatch(self):
        error = NativeImageError("backend_result_unconfirmed")
        backend = StubBackend(error)
        owner = self.owner(backend)

        first = await owner.execute(invocation(), before_dispatch=lambda: None)
        second = await owner.execute(invocation(), before_dispatch=lambda: None)

        self.assertEqual(first["state"], "unknown")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["state"], "unknown")
        self.assertEqual(backend.calls, 1)

    async def test_old_unknown_rows_are_not_rewritten(self):
        owner = self.owner(StubBackend(NativeImageError("backend_result_unconfirmed")))
        operation = await owner.execute(invocation(), before_dispatch=lambda: None)
        # Simulate a row written by the pre-fix build: unknown + a rejection code
        # that the fixed build would now classify as failed.
        connection = sqlite3.connect(owner.store.path)
        try:
            with connection:
                connection.execute("UPDATE operations SET state='unknown', "
                                   "error='native_backend_unconfigured', receipt='{}' WHERE id=?",
                                   (operation["id"],))
        finally:
            connection.close()
        stored = owner.store.get("scope-1", operation["id"])

        replayed = await owner.execute(invocation(), before_dispatch=lambda: None)

        self.assertEqual(stored["state"], "unknown")
        self.assertEqual(replayed["state"], "unknown")
        self.assertEqual(replayed["error"], "native_backend_unconfigured")
        self.assertEqual(replayed["receipt"], {})

    async def test_store_allows_dispatching_to_failed_but_freezes_final_rows(self):
        store = ImageStore(self.root / "store", None, identity_key="test-identity")
        request = invocation().document(())

        operation, fresh = store.begin(request)
        self.assertTrue(fresh)
        store.state("scope-1", operation["id"], "dispatching")
        failed = store.state("scope-1", operation["id"], "failed", error="native_endpoint_rejected")
        self.assertEqual(failed["state"], "failed")

        with self.assertRaises(NativeImageError) as caught:
            store.state("scope-1", operation["id"], "succeeded")
        self.assertEqual(caught.exception.code, "image_operation_final")

    async def test_restart_marks_a_dispatched_but_unanswered_operation_unknown(self):
        store = ImageStore(self.root / "restart", None, identity_key="test-identity")
        operation, _ = store.begin(invocation().document(()))
        store.state("scope-1", operation["id"], "dispatching")

        restarted = ImageStore(self.root / "restart", None, identity_key="test-identity")
        row = restarted.get("scope-1", operation["id"])

        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["error"], "runtime_restarted")

    async def test_a_second_owner_over_the_same_directory_steals_a_live_dispatch(self):
        # Documented round-2 hazard, deliberately not fixed here: constructing
        # another owner over the same directory (a second concurrent dashboard
        # test, or a provider rebuild) rewrites the first owner's in-flight row
        # to unknown/runtime_restarted and then invalidates it with
        # provider_generation_changed.  This test pins the current behaviour so a
        # later author has to change it on purpose.
        store = ImageStore(self.root / "shared", None, identity_key="test-identity")
        operation, _ = store.begin(invocation().document(()))
        store.state("scope-1", operation["id"], "dispatching")

        second = ImageStore(self.root / "shared", None, identity_key="test-identity")

        row = second.get("scope-1", operation["id"])
        self.assertEqual((row["state"], row["error"]), ("unknown", "runtime_restarted"))
        with self.assertRaises(NativeImageError) as caught:
            store.get("scope-1", operation["id"])
        self.assertEqual(caught.exception.code, "provider_generation_changed")

    async def test_cancellation_after_dispatch_stays_unknown(self):
        class Cancelling(StubBackend):
            async def execute(self, invocation, references, before_dispatch):
                self.calls += 1
                await before_dispatch()
                raise asyncio.CancelledError()

        owner = self.owner(Cancelling(NativeImageError("unused")))
        with self.assertRaises(asyncio.CancelledError):
            await owner.execute(invocation(), before_dispatch=lambda: None)

        row = owner.store.get("scope-1", owner.store.by_request("scope-1", "request-1")["id"])
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["error"], "execution_interrupted")


# --------------------------------------------------------------------------- #
# 4. Receipt contract and dashboard readability
# --------------------------------------------------------------------------- #

def provider_for(owner) -> NativeImageProvider:
    return NativeImageProvider(owner, package_version=PRODUCT_VERSION)


def request_document(owner) -> dict:
    return {"schema": native_contract.SCHEMA, "invocation_id": "invocation-1",
            "origin_key": "origin-1", "arguments_hash": "arguments-hash",
            "binding": {"capability": "image.generate", "provider": "image.native",
                        "version": PRODUCT_VERSION, "generation": owner.generation}}


class ReceiptContractTests(unittest.IsolatedAsyncioTestCase):
    ELEVEN_KEYS = {"schema", "invocation_id", "origin_key", "arguments_hash", "binding", "state",
                   "effect", "operation_id", "receipt_ref", "result", "artifacts"}

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    async def test_failed_operation_keeps_the_exact_eleven_key_receipt(self):
        rejection = output_routing.Rejected("native_endpoint_rejected", endpoint_id="primary",
                                            status=401, reason="unauthorized",
                                            detail="code=invalid_api_key", switchable=False)
        owner = NativeImageOwner(self.root, StubBackend(rejection))
        provider = provider_for(owner)
        operation = await owner.execute(invocation(), before_dispatch=lambda: None)
        request = request_document(owner)

        response = provider._response(request, operation)

        self.assertEqual(set(response), self.ELEVEN_KEYS)
        self.assertEqual((response["state"], response["effect"]), ("failed", "not_committed"))
        self.assertIsNone(response["result"])
        self.assertEqual(response["artifacts"], [])
        serialized = json.dumps(response)
        self.assertNotIn("diagnostics", serialized)
        self.assertNotIn(API_KEY, serialized)
        self.assertNotIn(CUSTOM_HEADER_PROMPT, serialized)
        parsed = native_contract.parse_result(response, request=request,
                                              side_effect=SideEffect.EXTERNAL_NETWORK)
        self.assertEqual((parsed.state, parsed.effect), ("failed", "not_committed"))

    async def test_unknown_operation_keeps_the_exact_eleven_key_receipt(self):
        owner = NativeImageOwner(self.root, StubBackend(NativeImageError("backend_result_unconfirmed")))
        provider = provider_for(owner)
        operation = await owner.execute(invocation(), before_dispatch=lambda: None)
        request = request_document(owner)

        response = provider._response(request, operation)

        self.assertEqual(set(response), self.ELEVEN_KEYS)
        self.assertEqual((response["state"], response["effect"]), ("unknown", "unknown"))
        parsed = native_contract.parse_result(response, request=request,
                                              side_effect=SideEffect.EXTERNAL_NETWORK)
        self.assertEqual((parsed.state, parsed.effect), ("unknown", "unknown"))


class DashboardReceiptTests(FakeHttpCase):
    def setUp(self):
        super().setUp()
        self.setUpTemporary()
        self.api = ImageExtensionAPI(data_root=self.root / "data",
                                     config_path=self.root / "image-config.json",
                                     version=PRODUCT_VERSION)
        self.addCleanup(self.api.close)
        current = self.api.output_configuration()
        self.api.save_output_configuration(current["revision"], {
            "enabled": True, "backend": "online_api", "endpoints": [endpoint()],
        })

    @property
    def root(self):
        return Path(self.temporary.name)

    async def test_dashboard_reports_failed_code_and_redacted_diagnostics(self):
        body = json.dumps({"error": {"message": f"bad key {API_KEY}", "code": "invalid_api_key",
                                     "param": "Authorization"}}).encode()
        self.http.reply(generate_url(), FakeResponse(body, status=401))

        result = await self.api.test_output_endpoint("primary", "op-key-1",
                                                     before_dispatch=lambda: None)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "native_endpoint_rejected")
        self.assertEqual(result["diagnostics"][0]["status"], 401)
        self.assertEqual(result["diagnostics"][0]["endpoint_id"], "primary")
        serialized = json.dumps(result)
        self.assertNotIn(API_KEY, serialized)
        self.assertNotIn(WORKSPACE_SECRET, serialized)
        self.assertIn("invalid_api_key", serialized)

    async def test_dashboard_keeps_unknown_for_a_server_error(self):
        self.http.reply(generate_url(), FakeResponse(openai_error(message="upstream boom"), status=500))

        result = await self.api.test_output_endpoint("primary", "op-key-2",
                                                     before_dispatch=lambda: None)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "backend_result_unconfirmed")
        self.assertEqual(result["diagnostics"][0]["status"], 500)
        self.assertIn("upstream boom", result["diagnostics"][0]["detail"])

    async def test_dashboard_replay_of_the_same_operation_is_read_only(self):
        rejection = FakeResponse(openai_error(), status=401)
        self.http.reply(generate_url(), rejection)

        first = await self.api.test_output_endpoint("primary", "op-key-3",
                                                    before_dispatch=lambda: None)
        second = await self.api.test_output_endpoint("primary", "op-key-3",
                                                     before_dispatch=lambda: None)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(len(self.http.posts()), 1)


if __name__ == "__main__":
    unittest.main()
