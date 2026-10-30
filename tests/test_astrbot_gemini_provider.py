"""AstrBot 的原生 Google GenAI provider 也必须能被 V2 文本适配器包装。"""

from types import SimpleNamespace as NS
import unittest

from companion_v2.adapters.astrbot.provider import AstrBotTextProvider, UsageTariff, _gemini_messages
from companion_v2.execution.models import ProviderBinding


class StubGeminiProvider:
    """只实现 AstrBot ProviderGoogleGenAI 对 V2 暴露的那部分表面。"""

    def __init__(self):
        self.provider_config = {"id": "gemini-native", "type": "googlegenai_chat_completion",
                                "model": "gemini-3.8-flash"}
        self.client = object()
        self.calls = []

    def get_model(self):
        return "gemini-3.8-flash"

    async def _query(self, payload, tools, *, request_max_retries=None):
        self.calls.append((payload, tools, request_max_retries))
        return NS(id="gemini-response-1", completion_text="好的", tools_call_name=[],
                  tools_call_ids=[], tools_call_args=[],
                  usage=NS(input_other=10, input_cached=2, output=5))


class StubContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider


def make_provider(streaming=False):
    native = StubGeminiProvider()
    provider = AstrBotTextProvider(
        StubContext(native), bridge=NS(),
        binding=ProviderBinding("model.text", "gemini-native", "4.29.1", "generation-1"),
        provider_id="gemini-native", version="4.29.1", provider_type=(StubGeminiProvider,),
        function_tool_type=object, tool_set_type=object, tariff=UsageTariff(3, 1, 9),
        maximum_output_tokens=1024, streaming=streaming)
    return provider, native


class UnrelatedProvider:
    """占位类：模拟未登记进 native_types 的宿主 provider 类型。"""


class StubAnthropicProvider:
    """未登记进 native_types 的 provider：只靠公共表面被接受。"""

    def __init__(self):
        self.provider_config = {"id": "anthropic-native", "type": "anthropic_chat_completion",
                                "model": "claude-x"}
        self.client = object()
        self.calls = []

    def get_model(self):
        return "claude-x"

    async def _query(self, payload, tools, *, request_max_retries=None):
        self.calls.append((payload, tools, request_max_retries))
        return NS(id="anthropic-response-1", completion_text="好的", tools_call_name=[],
                  tools_call_ids=[], tools_call_args=[],
                  usage=NS(input_other=3, input_cached=0, output=2))


class GeminiTextProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_provider_uses_its_own_transport(self):
        provider, _native = make_provider(streaming=True)
        self.assertEqual(provider.kind, "gemini")
        # 原生 Gemini 的流式协议与 V2 的流式校验器不兼容：构造时已降级。
        self.assertFalse(provider.streaming)
        local = provider.local_provider()
        self.assertEqual(local.transport, "astrbot_gemini")
        self.assertEqual(local.model, "gemini-3.8-flash")

    async def test_unregistered_native_provider_is_wrapped_by_surface(self):
        native = StubAnthropicProvider()
        provider = AstrBotTextProvider(
            StubContext(native), bridge=NS(),
            binding=ProviderBinding("model.text", "anthropic-native", "4.29.1", "generation-1"),
            provider_id="anthropic-native", version="4.29.1", provider_type=(UnrelatedProvider,),
            function_tool_type=object, tool_set_type=object, tariff=UsageTariff(3, 1, 9),
            maximum_output_tokens=1024)
        self.assertFalse(provider.verified_type)
        self.assertEqual(provider.kind, "native")
        self.assertEqual(provider.local_provider().transport, "astrbot_native")
        await provider.probe()
        payload, _tools, retries = native.calls[0]
        self.assertEqual(retries, 1)
        self.assertEqual(payload["max_tokens"], 8)

    async def test_probe_sends_the_gemini_payload(self):
        provider, native = make_provider()
        await provider.probe()
        payload, tools, retries = native.calls[0]
        self.assertIsNone(tools)
        self.assertEqual(retries, 1)
        self.assertEqual(payload["model"], "gemini-3.8-flash")
        self.assertEqual(payload["max_tokens"], 8)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Reply OK."}])

    async def test_token_usage_is_rated_like_openai_usage(self):
        provider, _native = make_provider()
        amount = provider._usage(NS(usage=NS(input_other=10, input_cached=2, output=5)))
        self.assertEqual(amount.input_tokens, 12)
        self.assertEqual(amount.cached_input_tokens, 2)
        self.assertEqual(amount.output_tokens, 5)
        self.assertEqual(amount.tokens, 17)

    async def test_tool_results_gain_the_function_name_for_gemini(self):
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "weather.lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "{\"ok\": true}"},
        ]
        enriched = _gemini_messages(messages)
        self.assertEqual(enriched[1]["name"], "weather.lookup")
        self.assertNotIn("name", messages[1])


if __name__ == "__main__":
    unittest.main()
