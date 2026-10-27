"""Round-3 F-3: the image 4xx switch matrix is scoped, not status-only.

Round 2 correctly stopped treating *every* 4xx as "the provider may have
executed" (a possibly-billed ``unknown``), but its STOP set
``{400, 401, 402, 403, 413, 422}`` was wider than its own declaration and shut
down multi-endpoint fallback for endpoint-local refusals:

* a 403 that says "this key has not enabled this model" is endpoint-local:
  every endpoint owns its own ``api_key`` and ``model`` (see
  ``output_configuration.py`` endpoint normalization);
* 413/422 are the endpoint's own payload limit and request-schema validation;
* a 400 request-shape error may be accepted by a different protocol adapter.

The matrix this suite pins (all 4xx below are definitive: the provider answered
and did not execute the generation, so the owner may record ``failed``):

* stop (account): 401, 402; plus 400/403/422 whose *detail* names an invalid
  credential, an account/billing condition, or a content-policy refusal.
  401/402 are credential/account states: probing other endpoints with the same
  broken key cannot help and can trip credential-abuse lockouts.  Content
  policy is deliberately a stop even on switchable statuses: the same prompt
  would be sent everywhere, so retrying is policy shopping, not availability.
* switch (endpoint): 400 request-shape, 403 entitlement, 404/405 path/method,
  413 payload, 422 schema.  A different endpoint has a different key, model
  entitlement, size limit and protocol version, so the same body may succeed.
* rate: 429 (one alternative, unchanged from round 2).
* unknown (never retried): 408/409/5xx/transport.  A provider may already have
  executed the request; the ``unknown`` invariant is asserted here again.

The suite is offline: ``aiohttp.ClientSession`` / ``FormData`` are replaced by
recording fakes and every owner store is a temporary SQLite file.
"""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from astrbot_host.native_extensions.image_native import output_routing
from astrbot_host.native_extensions.image_native.owner import NativeImageOwner
from astrbot_host.native_extensions.image_native.types import (
    ImageInvocation,
    NativeImageError,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n-round-3-image-body"
API_KEY = "sk-round3-primary-secret"
CUSTOM_HEADER_PROMPT = "A private round-3 prompt about a blue cup."


# --------------------------------------------------------------------------- #
# Recording aiohttp stand-ins (no sockets, no DNS)
# --------------------------------------------------------------------------- #

class _CountingContent:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.consumed = 0

    async def iter_chunked(self, size):
        for index in range(0, len(self.payload), size):
            block = self.payload[index:index + size]
            self.consumed += len(block)
            yield block


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
# Documents and bodies
# --------------------------------------------------------------------------- #

def endpoint(name: str = "primary", **overrides) -> dict:
    value = {
        "id": name, "name": name.title(), "enabled": True, "platform": "openai",
        "model": "gpt-image-1", "width": 1024, "height": 1024,
        "base_url": f"https://{name}.example.test/v1", "api_key": API_KEY,
        "custom_headers": {"X-Workspace": "round-3-workspace-secret"},
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


def invocation() -> ImageInvocation:
    return ImageInvocation("scope-1", "request-1", "invocation-1", "origin-1",
                           "generate", CUSTOM_HEADER_PROMPT)


def b64_success() -> FakeResponse:
    payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]})
    return FakeResponse(payload.encode("utf-8"))


def body(code="invalid_value", message="size must be one of 1024x1024",
         param="size", error_type="invalid_request_error") -> bytes:
    error = {"message": message, "type": error_type, "code": code}
    if param is not None:
        error["param"] = param
    return json.dumps({"error": error}).encode("utf-8")


# Named bodies used by the matrix.  The names describe the *cause*, which is
# what the classifier reads after the HTTP status.
ENTITLEMENT = body(code="model_not_enabled",
                   message="This API key has not enabled the requested model.",
                   param=None, error_type="permission_error")
SCHEMA = body(code="invalid_parameter", message="size does not match the model contract",
              param="size")
PAYLOAD = body(code="payload_too_large", message="Request body exceeds this endpoint limit",
               param=None, error_type="invalid_request_error")
REQUEST_SHAPE = body(code="invalid_value", message="quality 'standard' is not supported",
                     param="quality")
CONTENT = body(code="content_policy_violation",
               message="Your request was rejected by the safety system.",
               param=None, error_type="invalid_request_error")
CREDENTIAL = body(code="invalid_api_key", message="Incorrect API key provided.",
                  param="Authorization", error_type="authentication_error")
ACCOUNT = body(code="insufficient_quota",
               message="You exceeded your current quota; check your plan and billing details.",
               param=None, error_type="insufficient_quota")
SERVER = body(code="server_error", message="upstream worker crashed", param=None)


class FakeHttpCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = FakeHTTP()
        patches = [mock.patch.object(output_routing.aiohttp, "ClientSession", self.http.session),
                   mock.patch.object(output_routing.aiohttp, "FormData", _RecordingForm)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def classify(self, status: int, payload: bytes):
        self.http.calls.clear()
        self.http.reply(generate_url(), FakeResponse(payload, status=status))
        backend = output_routing.OnlineImageBackend(endpoint())
        with self.assertRaises(NativeImageError) as caught:
            await backend.execute(invocation(), (), lambda: None)
        return caught.exception

    async def routing_error(self, settings):
        backend = output_routing.OutputRoutingBackend(settings, Path(self.temporary.name))
        with self.assertRaises(NativeImageError) as caught:
            await backend.execute(invocation(), (), lambda: None)
        return caught.exception

    def setUpTemporary(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)


# --------------------------------------------------------------------------- #
# 1. Declared matrix and pure classifier
# --------------------------------------------------------------------------- #

class FailureMatrixDeclarationTests(unittest.TestCase):
    def test_declared_status_sets_are_disjoint_and_complete(self):
        stop = set(output_routing.STOP_FAILURE_STATUSES)
        switch = set(output_routing.SWITCH_FAILURE_STATUSES)
        rate = set(output_routing.RATE_LIMIT_STATUSES)

        self.assertEqual(stop, {401, 402})
        self.assertEqual(switch, {400, 403, 404, 405, 413, 422})
        self.assertEqual(rate, {429})
        self.assertEqual(stop & switch, set())
        self.assertEqual(stop & rate, set())
        self.assertEqual(switch & rate, set())
        self.assertEqual(set(output_routing.DEFINITIVE_FAILURE_STATUSES), stop | switch | rate)

    def test_every_switch_status_defaults_to_endpoint_scope(self):
        for status in sorted(output_routing.SWITCH_FAILURE_STATUSES):
            with self.subTest(status=status):
                self.assertEqual(output_routing.failure_scope(status, ""),
                                 output_routing.SCOPE_ENDPOINT)

    def test_account_and_rate_scopes_are_status_fixed(self):
        for status in (401, 402):
            with self.subTest(status=status):
                self.assertEqual(output_routing.failure_scope(status, ""),
                                 output_routing.SCOPE_ACCOUNT)
        self.assertEqual(output_routing.failure_scope(429, ""),
                         output_routing.SCOPE_RATE)

    def test_global_causes_override_switchable_statuses(self):
        cases = (
            (400, CONTENT.decode(), output_routing.SCOPE_CONTENT),
            (400, CREDENTIAL.decode(), output_routing.SCOPE_ACCOUNT),
            (400, ACCOUNT.decode(), output_routing.SCOPE_ACCOUNT),
            (403, CONTENT.decode(), output_routing.SCOPE_CONTENT),
            (403, CREDENTIAL.decode(), output_routing.SCOPE_ACCOUNT),
            (403, ACCOUNT.decode(), output_routing.SCOPE_ACCOUNT),
            (422, CONTENT.decode(), output_routing.SCOPE_CONTENT),
        )
        for status, detail, expected in cases:
            with self.subTest(status=status, expected=expected):
                detail = output_routing._error_detail(detail.encode("utf-8"))
                self.assertEqual(output_routing.failure_scope(status, detail), expected)

    def test_endpoint_causes_do_not_match_global_markers(self):
        for status, payload in ((400, REQUEST_SHAPE), (403, ENTITLEMENT),
                                (413, PAYLOAD), (422, SCHEMA)):
            with self.subTest(status=status):
                detail = output_routing._error_detail(payload)
                self.assertEqual(output_routing.failure_scope(status, detail),
                                 output_routing.SCOPE_ENDPOINT)

    def test_non_4xx_and_indeterminate_statuses_are_unconfirmed(self):
        for status in (408, 409, 418, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertEqual(output_routing.failure_scope(status, ""),
                                 output_routing.SCOPE_UNCONFIRMED)


# --------------------------------------------------------------------------- #
# 2. Per-endpoint classification (OnlineImageBackend)
# --------------------------------------------------------------------------- #

class EndpointScopeClassificationTests(FakeHttpCase):
    async def test_entitlement_403_is_definitive_but_switchable(self):
        error = await self.classify(403, ENTITLEMENT)

        self.assertIsInstance(error, output_routing.Rejected)
        self.assertFalse(error.unconfirmed)
        self.assertTrue(error.switchable)
        self.assertEqual(error.kind, output_routing.REJECTED_ENDPOINT)
        self.assertEqual(error.code, "native_endpoint_rejected")
        self.assertEqual(error.diagnostics[0]["scope"], "endpoint")
        self.assertEqual(error.diagnostics[0]["status"], 403)

    async def test_payload_413_and_schema_422_are_switchable(self):
        for status, payload in ((413, PAYLOAD), (422, SCHEMA)):
            with self.subTest(status=status):
                error = await self.classify(status, payload)
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertFalse(error.unconfirmed)
                self.assertTrue(error.switchable)
                self.assertEqual(error.kind, output_routing.REJECTED_ENDPOINT)
                self.assertEqual(error.diagnostics[0]["scope"], "endpoint")

    async def test_request_shape_400_is_switchable(self):
        error = await self.classify(400, REQUEST_SHAPE)

        self.assertIsInstance(error, output_routing.Rejected)
        self.assertFalse(error.unconfirmed)
        self.assertTrue(error.switchable)
        self.assertEqual(error.kind, output_routing.REJECTED_ENDPOINT)
        self.assertEqual(error.diagnostics[0]["scope"], "endpoint")

    async def test_unknown_or_empty_switchable_bodies_default_to_endpoint(self):
        for status in (400, 403, 422):
            with self.subTest(status=status):
                error = await self.classify(status, b"")
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertTrue(error.switchable)
                self.assertEqual(error.diagnostics[0]["scope"], "endpoint")

    async def test_account_statuses_are_never_switchable(self):
        for status in (401, 402):
            with self.subTest(status=status):
                error = await self.classify(status, b"")
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertFalse(error.unconfirmed)
                self.assertFalse(error.switchable)
                self.assertEqual(error.kind, output_routing.REJECTED_STOP)
                self.assertEqual(error.diagnostics[0]["scope"], "account")

    async def test_content_policy_stops_even_on_a_switchable_status(self):
        for status, payload in ((400, CONTENT), (403, CONTENT), (422, CONTENT)):
            with self.subTest(status=status):
                error = await self.classify(status, payload)
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertFalse(error.unconfirmed)
                self.assertFalse(error.switchable)
                self.assertEqual(error.kind, output_routing.REJECTED_STOP)
                self.assertEqual(error.diagnostics[0]["scope"], "content")

    async def test_credential_and_billing_bodies_stop_switchable_statuses(self):
        for status, payload, expected in (
                (400, CREDENTIAL, "account"), (403, CREDENTIAL, "account"),
                (400, ACCOUNT, "account"), (403, ACCOUNT, "account")):
            with self.subTest(status=status):
                error = await self.classify(status, payload)
                self.assertIsInstance(error, output_routing.Rejected)
                self.assertFalse(error.switchable)
                self.assertEqual(error.kind, output_routing.REJECTED_STOP)
                self.assertEqual(error.diagnostics[0]["scope"], expected)

    async def test_indeterminate_statuses_stay_unconfirmed_and_unswitched(self):
        for status in (408, 409, 500, 502, 504):
            with self.subTest(status=status):
                error = await self.classify(status, SERVER if status >= 500 else b"")
                self.assertNotIsInstance(error, output_routing.Rejected)
                self.assertTrue(error.unconfirmed)
                self.assertEqual(error.code, "backend_result_unconfirmed")
                self.assertEqual(error.diagnostics[0]["scope"], "unconfirmed")

    async def test_429_keeps_its_rate_scope(self):
        error = await self.classify(429, body(message="slow down"))

        self.assertIsInstance(error, output_routing.Rejected)
        self.assertTrue(error.switchable)
        self.assertEqual(error.kind, output_routing.REJECTED_RATE)
        self.assertEqual(error.code, "backend_rate_limited")
        self.assertEqual(error.diagnostics[0]["scope"], "rate")


# --------------------------------------------------------------------------- #
# 3. Routing matrix (OutputRoutingBackend)
# --------------------------------------------------------------------------- #

class RoutingFallbackTests(FakeHttpCase):
    def setUp(self):
        super().setUp()
        self.setUpTemporary()
        self.primary, self.secondary, self.tertiary = (
            endpoint("primary"), endpoint("secondary"), endpoint("tertiary"))

    def settings(self, *names):
        by_name = {"primary": self.primary, "secondary": self.secondary, "tertiary": self.tertiary}
        ordered = {operation: [f"online:{name}" for name in names]
                   for operation in ("generate", "edit")}
        return route_settings([by_name[name] for name in names], ordered)

    async def test_entitlement_403_falls_back_and_succeeds(self):
        self.http.reply(generate_url("primary"), FakeResponse(ENTITLEMENT, status=403))
        self.http.reply(generate_url("secondary"), b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(), (), lambda: None)

        self.assertEqual(result.images, (PNG_BYTES,))
        self.assertEqual(result.receipt["endpoint_id"], "secondary")
        self.assertEqual(len(self.http.posts()), 2)
        self.assertNotIn("diagnostics", json.dumps(result.receipt))

    async def test_413_falls_back_and_succeeds(self):
        self.http.reply(generate_url("primary"), FakeResponse(PAYLOAD, status=413))
        self.http.reply(generate_url("secondary"), b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(), (), lambda: None)

        self.assertEqual(result.receipt["endpoint_id"], "secondary")
        self.assertEqual(len(self.http.posts()), 2)

    async def test_422_falls_back_and_succeeds(self):
        self.http.reply(generate_url("primary"), FakeResponse(SCHEMA, status=422))
        self.http.reply(generate_url("secondary"), b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(), (), lambda: None)

        self.assertEqual(result.receipt["endpoint_id"], "secondary")
        self.assertEqual(len(self.http.posts()), 2)

    async def test_request_shape_400_falls_back_and_succeeds(self):
        self.http.reply(generate_url("primary"), FakeResponse(REQUEST_SHAPE, status=400))
        self.http.reply(generate_url("secondary"), b64_success())

        backend = output_routing.OutputRoutingBackend(self.settings("primary", "secondary"),
                                                      Path(self.temporary.name))
        result = await backend.execute(invocation(), (), lambda: None)

        self.assertEqual(result.receipt["endpoint_id"], "secondary")
        self.assertEqual(len(self.http.posts()), 2)

    async def test_account_refusals_never_touch_the_secondary(self):
        for status in (401, 402):
            with self.subTest(status=status):
                self.http.calls.clear()
                self.http.reply(generate_url("primary"), FakeResponse(b"", status=status))
                self.http.reply(generate_url("secondary"), b64_success())

                error = await self.routing_error(self.settings("primary", "secondary"))

                self.assertEqual(error.code, "native_endpoint_rejected")
                self.assertFalse(error.unconfirmed)
                self.assertEqual(len(self.http.posts()), 1)
                self.assertEqual(error.diagnostics[0]["scope"], "account")

    async def test_content_policy_does_not_shop_for_another_endpoint(self):
        for status in (400, 403, 422):
            with self.subTest(status=status):
                self.http.calls.clear()
                self.http.reply(generate_url("primary"), FakeResponse(CONTENT, status=status))
                self.http.reply(generate_url("secondary"), b64_success())

                error = await self.routing_error(self.settings("primary", "secondary"))

                self.assertEqual(error.code, "native_endpoint_rejected")
                self.assertFalse(error.unconfirmed)
                self.assertEqual(len(self.http.posts()), 1)
                self.assertEqual(error.diagnostics[0]["scope"], "content")

    async def test_credential_403_does_not_touch_the_secondary(self):
        self.http.reply(generate_url("primary"), FakeResponse(CREDENTIAL, status=403))
        self.http.reply(generate_url("secondary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "native_endpoint_rejected")
        self.assertEqual(len(self.http.posts()), 1)
        self.assertEqual(error.diagnostics[0]["scope"], "account")

    async def test_all_endpoint_scoped_refusals_report_endpoints_rejected(self):
        self.http.reply(generate_url("primary"), FakeResponse(ENTITLEMENT, status=403))
        self.http.reply(generate_url("secondary"), FakeResponse(PAYLOAD, status=413))

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "native_endpoints_rejected")
        self.assertFalse(error.unconfirmed)
        self.assertEqual([item["status"] for item in error.diagnostics], [403, 413])
        self.assertEqual([item["scope"] for item in error.diagnostics],
                         ["endpoint", "endpoint"])
        self.assertEqual(len(self.http.posts()), 2)

    async def test_unknown_after_a_switch_is_still_never_retried(self):
        self.http.reply(generate_url("primary"), FakeResponse(REQUEST_SHAPE, status=400))
        self.http.reply(generate_url("secondary"), FakeResponse(SERVER, status=500))
        self.http.reply(generate_url("tertiary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary", "tertiary"))

        self.assertEqual(error.code, "backend_result_unconfirmed")
        self.assertTrue(error.unconfirmed)
        self.assertEqual(len(self.http.posts()), 2)
        self.assertEqual(self.http.posts()[-1]["url"], generate_url("secondary"))

    async def test_server_error_as_the_first_answer_is_never_retried(self):
        self.http.reply(generate_url("primary"), FakeResponse(SERVER, status=500))
        self.http.reply(generate_url("secondary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary"))

        self.assertEqual(error.code, "backend_result_unconfirmed")
        self.assertTrue(error.unconfirmed)
        self.assertEqual(len(self.http.posts()), 1)

    async def test_switching_403_then_401_stops_with_both_diagnostics(self):
        self.http.reply(generate_url("primary"), FakeResponse(ENTITLEMENT, status=403))
        self.http.reply(generate_url("secondary"), FakeResponse(b"", status=401))
        self.http.reply(generate_url("tertiary"), b64_success())

        error = await self.routing_error(self.settings("primary", "secondary", "tertiary"))

        self.assertEqual(error.code, "native_endpoint_rejected")
        self.assertEqual([item["status"] for item in error.diagnostics], [403, 401])
        self.assertEqual([item["scope"] for item in error.diagnostics],
                         ["endpoint", "account"])
        self.assertEqual(len(self.http.posts()), 2)


# --------------------------------------------------------------------------- #
# 4. Owner terminal state: no switchable 4xx may become unknown
# --------------------------------------------------------------------------- #

class OwnerTerminalStateTests(FakeHttpCase):
    def setUp(self):
        super().setUp()
        self.setUpTemporary()
        self.primary, self.secondary = endpoint("primary"), endpoint("secondary")

    def settings(self):
        routes = {operation: ["online:primary", "online:secondary"]
                  for operation in ("generate", "edit")}
        return route_settings([self.primary, self.secondary], routes)

    async def execute(self):
        backend = output_routing.OutputRoutingBackend(self.settings(),
                                                      Path(self.temporary.name))
        owner = NativeImageOwner(Path(self.temporary.name), backend)
        return await owner.execute(invocation(), before_dispatch=lambda: None)

    async def test_exhausted_switchable_refusals_are_failed_not_unknown(self):
        self.http.reply(generate_url("primary"), FakeResponse(ENTITLEMENT, status=403))
        self.http.reply(generate_url("secondary"), FakeResponse(SCHEMA, status=422))

        operation = await self.execute()

        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error"], "native_endpoints_rejected")
        self.assertEqual([item["status"] for item in operation["receipt"]["diagnostics"]],
                         [403, 422])

    async def test_account_refusal_is_failed(self):
        self.http.reply(generate_url("primary"), FakeResponse(b"", status=401))
        self.http.reply(generate_url("secondary"), b64_success())

        operation = await self.execute()

        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error"], "native_endpoint_rejected")
        self.assertEqual(operation["receipt"]["diagnostics"][0]["scope"], "account")
        self.assertEqual(len(self.http.posts()), 1)

    async def test_content_refusal_is_failed_not_unknown(self):
        self.http.reply(generate_url("primary"), FakeResponse(CONTENT, status=400))
        self.http.reply(generate_url("secondary"), b64_success())

        operation = await self.execute()

        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error"], "native_endpoint_rejected")
        self.assertEqual(operation["receipt"]["diagnostics"][0]["scope"], "content")

    async def test_server_error_remains_unknown(self):
        self.http.reply(generate_url("primary"), FakeResponse(SERVER, status=500))
        self.http.reply(generate_url("secondary"), b64_success())

        operation = await self.execute()

        self.assertEqual(operation["state"], "unknown")
        self.assertEqual(operation["error"], "backend_result_unconfirmed")
        self.assertEqual(operation["receipt"]["diagnostics"][0]["scope"], "unconfirmed")


# --------------------------------------------------------------------------- #
# 5. Diagnostics contract: the new scope key is bounded and secret-free
# --------------------------------------------------------------------------- #

class DiagnosticsContractTests(FakeHttpCase):
    async def test_scope_is_present_in_every_diagnostic_and_never_leaks(self):
        payload = body(code="invalid_request_error",
                       message=f"bad {API_KEY} for {CUSTOM_HEADER_PROMPT}")
        self.http.reply(generate_url(), FakeResponse(payload, status=400))

        backend = output_routing.OnlineImageBackend(endpoint())
        with self.assertRaises(NativeImageError) as caught:
            await backend.execute(invocation(), (), lambda: None)

        record = caught.exception.diagnostics[0]
        self.assertEqual(set(record), {"endpoint_id", "status", "reason", "detail",
                                       "retry_after", "scope"})
        self.assertEqual(record["scope"], "endpoint")
        serialized = json.dumps(record)
        self.assertNotIn(API_KEY, serialized)
        self.assertNotIn(CUSTOM_HEADER_PROMPT, serialized)


if __name__ == "__main__":
    unittest.main()
