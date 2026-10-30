"""Capture private memory replies before JSON decoding or host text repair."""

import json

from companion_v2.model_gateway.models import CapturedModelOutput

from .response_diagnostics import native_finish_reason


# CapturedModelOutput names the same endings with its own vocabulary. Anything the
# provider leaves unclassified stays "stop", which is what this path reported
# before the candidate's finish reason could be read at all.
_CAPTURED_FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    "content_filter": "refusal",
    "tool_calls": "invalid",
    "function_call": "invalid",
}


def explicit_refusal(completion):
    choices = getattr(completion, "choices", ())
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        return False
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None or getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
        return False
    return bool(getattr(message, "refusal", None)) or getattr(choice, "finish_reason", None) == "content_filter"


def response_format(request):
    if request.response_format == "text":
        return None
    if request.response_format == "json_object":
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {
        "name": "content_authoring" if request.output_format == "content_authoring" else "memory_reflection", "strict": True,
        "schema": json.loads(request.response_schema_json),
    }}


def capture(completion):
    choices = getattr(completion, "choices", ())
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        # 原生 Gemini 等 provider 只暴露归一化后的 completion_text。
        body = getattr(completion, "completion_text", None)
        if not isinstance(body, str):
            return CapturedModelOutput("", "invalid")
        if len(body) > 65536:
            return CapturedModelOutput(body[:65536], "oversized")
        if getattr(completion, "tools_call_args", None):
            return CapturedModelOutput(body, "invalid")
        # 触到输出上限的回答不是「说完了」：把它记成 stop 会让上层把半句正文当成
        # 完整回答交付出去。candidate 自己给出的结束原因才是这里的依据。
        return CapturedModelOutput(body, _CAPTURED_FINISH_REASONS.get(native_finish_reason(completion), "stop"))
    choice = choices[0]
    message = getattr(choice, "message", None)
    body = getattr(message, "content", None)
    reason = getattr(choice, "finish_reason", None)
    if not isinstance(body, str):
        body = ""
    if len(body) > 65536:
        return CapturedModelOutput(body[:65536], "oversized")
    if explicit_refusal(completion):
        return CapturedModelOutput(body, "refusal")
    if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
        return CapturedModelOutput(body, "invalid")
    if reason not in ("stop", "length"):
        reason = "invalid"
    return CapturedModelOutput(body, reason)
