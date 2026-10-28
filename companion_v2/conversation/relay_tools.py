"""First-party model tool declarations for controlled cross-window relaying."""

from companion_v2.capabilities.models import CapabilityPackageDescriptor
from companion_v2.conversation.external_tools import ExtensionToolDescriptor
from companion_v2.execution.models import ProviderBinding, SideEffect, encode
from companion_v2.model_gateway.models import ToolSpec


EXTENSION = "conversation.relay"
PACKAGE_ID = "companion_v2.conversation.relay"
PUBLISHER_ID = "astrbot_plugin_private_companion"
PACKAGE_VERSION = "1.0.0"
ARTIFACT_HASH = "included-conversation-relay-1.0.0"
PROVIDER = "companion.relay"
GENERATION = "included-1"
DATASET_OWNER = "conversation.relay.audit"

PROPOSE_ACTION = "relay_message"
SEND_ACTION = "relay_send"
PROPOSE_MANY_ACTION = "relay_message_many"
SEND_MANY_ACTION = "relay_send_many"
RELAY_ACTIONS = frozenset((PROPOSE_ACTION, SEND_ACTION, PROPOSE_MANY_ACTION, SEND_MANY_ACTION))


def package_descriptor():
    return CapabilityPackageDescriptor(
        extension_id=EXTENSION,
        package_id=PACKAGE_ID,
        publisher_id=PUBLISHER_ID,
        package_version=PACKAGE_VERSION,
        artifact_hash=ARTIFACT_HASH,
        capabilities=tuple(sorted(RELAY_ACTIONS)),
        dataset_owners=(DATASET_OWNER,),
    )


def _descriptor(action, description, properties, required, side_effect):
    schema = encode({"type": "object", "additionalProperties": False,
                     "properties": properties, "required": list(required)})
    return ExtensionToolDescriptor(
        ToolSpec(action, description, schema),
        package_descriptor(),
        ProviderBinding(PROPOSE_ACTION if action in {PROPOSE_ACTION, PROPOSE_MANY_ACTION} else SEND_ACTION,
                        PROVIDER, PACKAGE_VERSION, GENERATION),
        DATASET_OWNER,
        side_effect=side_effect,
        timeout_seconds=20,
    )


def tool_descriptors():
    return (
        _descriptor(
            PROPOSE_ACTION,
            "准备一条跨窗口私聊或群聊递话，不会发送。只在用户明确要求代为转达时调用，并沿用用户给出的目标。"
            "text 是实际要发的正文：style=original 时逐字保留；soft/persona 时由你按风格先在 text 中改写。"
            "群聊目标必须命中管理员配置的群白名单；mention 可选填写要 @ 的群友 QQ、昵称或群名片。"
            "delay_until_seen 只用于群聊且必须同时提供 mention；need_receipt 只用于私聊。"
            "返回 awaiting_confirmation 时，把目标、正文和确认编号告诉用户，等待用户明确回复确认发送后再调用发送工具；"
            "返回 ready 时可以在当前轮继续调用发送工具。",
            {"destination_kind": {"type": "string", "enum": ["private", "group"],
                                  "description": "private 表示私聊，group 表示群聊；省略时按私聊处理。"},
             "destination_id": {"type": "string", "description": "私聊账号 ID 或群号。"},
             "text": {"type": "string", "description": "要转达的正文；按 style 由你在调用前完成措辞，工具不会二次改写。"},
             "mention": {"type": "string", "description": "群聊可选：要 @ 的群友 QQ、昵称或群名片。"},
             "style": {"type": "string", "enum": ["original", "soft", "persona"],
                       "description": "original 逐字保留；soft 更委婉；persona 按角色语气。省略时使用配置默认。"},
             "delay_until_seen": {"type": "boolean", "description": "群聊可选：等目标群友在该群出现后再发送。"},
             "need_receipt": {"type": "boolean", "description": "私聊可选：等待对方下一次私聊回复并带回当前会话。"},
             "confirm_before_report": {"type": "boolean", "description": "私聊可选：带回回复前先征得收件人同意。"}},
            ("destination_id", "text"), SideEffect.NONE,
        ),
        _descriptor(
            PROPOSE_MANY_ACTION,
            "一次准备多条同文跨窗口/跨群递话，不会发送。目标数量受配置的多目标上限限制。"
            "每个目标都会得到独立确认编号；随后可用 relay_send_many 一次发送，也可分别用 relay_send 发送。"
            "style 与 relay_message 相同，soft/persona 时由你在 text 中先改写。",
            {"destination_kind": {"type": "string", "enum": ["private", "group"]},
             "destination_ids": {"type": "string", "description": "逗号或换行分隔的私聊账号 ID / 群号。"},
             "text": {"type": "string", "description": "实际要发送的正文。"},
             "mention": {"type": "string", "description": "群聊可选：统一 @ 的群友。"},
             "style": {"type": "string", "enum": ["original", "soft", "persona"]},
             "delay_until_seen": {"type": "boolean"},
             "need_receipt": {"type": "boolean"},
             "confirm_before_report": {"type": "boolean"}},
            ("destination_ids", "text"), SideEffect.NONE,
        ),
        _descriptor(
            SEND_ACTION,
            "发送 relay_message 返回的那一条待发送递话。必须沿用同一确认编号、目标类型、目标、正文、风格和 @ 对象。"
            "需要确认时，confirmed 只能在当前用户消息明确确认后设为 true；参数本身不构成确认。"
            "如果上次结果未知，不要再次调用或换编号重发。",
            {"operation_id": {"type": "string", "description": "relay_message 返回的确认编号。"},
             "destination_kind": {"type": "string", "enum": ["private", "group"],
                                  "description": "与待发送操作完全一致的目标类型。"},
             "destination_id": {"type": "string", "description": "与待发送操作完全一致的私聊账号 ID 或群号。"},
             "text": {"type": "string", "description": "与待发送操作完全一致的正文。"},
             "style": {"type": "string", "enum": ["original", "soft", "persona"],
                       "description": "与准备操作完全一致的转述风格。"},
             "mention": {"type": "string", "description": "群聊可选：与待发送操作完全一致的 @ 对象 ID。"},
             "delay_until_seen": {"type": "boolean", "description": "群聊可选：与准备操作一致的延迟开关。"},
             "need_receipt": {"type": "boolean", "description": "私聊可选：与准备操作一致的回执开关。"},
             "confirm_before_report": {"type": "boolean", "description": "私聊可选：与准备操作一致的回报前确认。"},
             "confirmed": {"type": "boolean", "description": "仅在当前用户消息明确确认后为 true；否则为 false。"}},
            ("operation_id", "destination_id", "text", "confirmed"), SideEffect.MESSAGE_DELIVERY,
        ),
        _descriptor(
            SEND_MANY_ACTION,
            "一次发送 relay_message_many 返回的多条待发送递话。每条必须沿用返回的确认编号、目标类型、目标、正文、"
            "风格、@ 对象和延迟/回执开关，不能只填目标；其中一个结果未知时不会重发它，也不会让其他目标回滚。",
            {"operations": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "operation_id": {"type": "string"},
                    "destination_kind": {"type": "string", "enum": ["private", "group"]},
                    "destination_id": {"type": "string"},
                    "text": {"type": "string"},
                    "style": {"type": "string", "enum": ["original", "soft", "persona"]},
                    "mention": {"type": "string"},
                    "delay_until_seen": {"type": "boolean"},
                    "need_receipt": {"type": "boolean"},
                    "confirm_before_report": {"type": "boolean"}},
                "required": ["operation_id", "destination_id", "text"]}},
             "confirmed": {"type": "boolean", "description": "仅在当前用户消息明确确认后为 true。"}},
            ("operations", "confirmed"), SideEffect.MESSAGE_DELIVERY,
        ),
    )

__all__ = ["ARTIFACT_HASH", "DATASET_OWNER", "EXTENSION", "GENERATION", "PACKAGE_ID", "PACKAGE_VERSION",
           "PROPOSE_ACTION", "PROPOSE_MANY_ACTION", "PROVIDER", "PUBLISHER_ID", "RELAY_ACTIONS",
           "SEND_ACTION", "SEND_MANY_ACTION", "package_descriptor", "tool_descriptors"]
