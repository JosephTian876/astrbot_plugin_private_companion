"""Best-effort native TTS for an already sealed conversation part."""

import asyncio
import base64
import inspect
import re
import logging
from pathlib import Path
import struct
from dataclasses import replace

from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from companion_v2.conversation.voice_reply import (
    VoiceReplyPolicy, requested_voice_language, with_voice_override,
)
from companion_v2.foundation.refs import ScopeKind


logger = logging.getLogger(__name__)
MAX_VOICE_BYTES = 8_000_000


def tts_provider_catalog(context):
    """Describe loaded TTS providers without exposing credentials."""
    try:
        providers = tuple(context.get_all_tts_providers())
    except Exception:
        return {"state": "unavailable", "providers": []}
    entries = {}
    for provider in providers:
        try:
            metadata = provider.meta()
            identifier = metadata.id
        except Exception:
            continue
        if type(identifier) is not str or not identifier.strip() or len(identifier) > 128:
            continue
        config = getattr(provider, "provider_config", None)
        config = config if isinstance(config, dict) else {}
        item = {"id": identifier.strip()}
        model = config.get("model")
        if not (type(model) is str and model.strip()):
            model = getattr(metadata, "model", None)
        if type(model) is str and model.strip() and len(model.strip()) <= 160:
            item["model"] = model.strip()
        for key in ("fishaudio-tts-character", "character", "voice", "voice_name", "azure_tts_voice",
                    "dashscope_tts_voice", "gemini_tts_voice_name", "elevenlabs-tts-voice-id"):
            value = config.get(key)
            if type(value) is str and value.strip() and len(value) <= 160:
                item["voice"] = value.strip()
                break
        entries[identifier.strip()] = item
    return {"state": "ready", "providers": [entries[key] for key in sorted(entries)]}


def _provider_with_overrides(provider, model="", voice=""):
    """Clone one loaded provider with per-turn model/voice fields when possible."""
    if not model and not voice:
        return provider
    config = getattr(provider, "provider_config", None)
    settings = getattr(provider, "provider_settings", None)
    if not isinstance(config, dict):
        return provider
    candidate = dict(config)
    if model:
        candidate["model"] = model
    if voice:
        keys = [key for key in candidate if key.endswith("-voice") or key.endswith("_voice")
                or key in ("voice", "voice_name", "character")]
        fish_key = "fishaudio-tts-character"
        if fish_key in candidate or any("fishaudio" in key.lower() for key in candidate):
            if re.fullmatch(r"[a-fA-F0-9]{32}", voice):
                candidate["fishaudio-tts-reference-id"] = voice
            else:
                candidate[fish_key] = voice
        elif keys:
            candidate[keys[0]] = voice
        else:
            return provider
    try:
        return type(provider)(candidate, dict(settings) if isinstance(settings, dict) else {})
    except Exception:
        logger.warning("[CompanionV2] TTS per-turn provider override unavailable type=%s",
                       type(provider).__name__)
        return provider


def _voice_alias_compact(value):
    return re.sub(r"[^a-z0-9.]+", "", str(value or "").casefold())


def _voice_aliases(value):
    compact = _voice_alias_compact(value)
    if not compact:
        return set()
    aliases = {compact}
    no_pro = compact.replace("pro", "")
    aliases.add(no_pro)
    if no_pro.startswith("s") and len(no_pro) > 1:
        aliases.add(no_pro[1:])
    if compact.startswith("s") and len(compact) > 1:
        aliases.add(compact[1:])
    return {alias for alias in aliases if len(alias) >= 3}


def admin_voice_override(policy, text, catalog):
    """Apply an explicit core-admin TTS model/voice/provider request to one turn."""
    if not isinstance(text, str) or not isinstance(catalog, dict):
        return None
    if not re.search(r"(?:语音|朗读|念|音色|声音|模型|tts|voice|model|speak|read)", text, re.I):
        return None
    if re.search(r"(?:不要|别|取消|停止|不用|avoid|don't|do not).{0,18}(?:语音|朗读|音色|声音|tts|voice|speak|read)", text, re.I):
        return None
    compact_text = _voice_alias_compact(text)
    selected = None
    selected_length = 0
    for item in catalog.get("providers") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        aliases = set()
        for value in (item.get("id"), item.get("model"), item.get("voice"),
                      item.get("provider_type"), item.get("label")):
            aliases.update(_voice_aliases(value))
        for alias in aliases:
            if len(alias) > selected_length and alias in compact_text:
                selected, selected_length = item, len(alias)
    model_hint = ""
    voice_hint = ""
    model_match = (re.search(r"(?:模型|model)\s*[:：=]?\s*([A-Za-z0-9._:-]+)", text, re.I)
                   or re.search(r"([A-Za-z0-9._:-]+)\s*(?:模型|model)", text, re.I))
    if model_match:
        model_hint = model_match.group(1).strip()
    voice_match = re.search(r"(?:音色|声音|角色|voice)\s*[:：=]?\s*([^\s，。；;]{1,80})", text, re.I)
    if voice_match:
        voice_hint = voice_match.group(1).strip()
    if selected is None and not model_hint and not voice_hint:
        return None
    if selected is not None and not re.search(
            r"(?:用|使用|换成|切换|指定|来|说|回复|朗读|念|use|switch|speak|read)", text, re.I):
        return None
    base = policy if isinstance(policy, VoiceReplyPolicy) else VoiceReplyPolicy()
    language = requested_voice_language(text)
    if not language:
        language_names = {
            "ja": r"(?:日语|日語|日文|日本语|日本語|japanese)",
            "en": r"(?:英语|英語|英文|english)",
            "zh": r"(?:中文|汉语|漢語|普通话|国语|國語|chinese)",
        }
        for code, names in language_names.items():
            if re.search(rf"(?:用|以|换成|切换(?:到|成)?)\s*(?:{names})", text, re.I):
                language = code
                break
    forced = replace(base, language=language or base.language,
        trigger_mode="always", probability=1.0, private_probability=-1.0,
        group_probability=-1.0, cooldown_seconds=0,
        private_cooldown_seconds=-1, group_cooldown_seconds=-1)
    return with_voice_override(forced,
        provider_id=selected.get("id", "") if selected else "",
        model=model_hint if selected is None else "",
        voice=voice_hint if selected is None else "")


def _finalize_streamed_wav(audio):
    """Replace a streaming WAV's placeholder lengths with received lengths."""
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio
    riff_size = struct.unpack_from("<I", audio, 4)[0]
    cursor = 12
    while cursor + 8 <= len(audio):
        size = struct.unpack_from("<I", audio, cursor + 4)[0]
        if (audio[cursor:cursor + 4] == b"data" and size in {0xFFFFFF00, 0xFFFFFFFF}
                and riff_size > len(audio) - 8):
            finalized = bytearray(audio)
            struct.pack_into("<I", finalized, 4, len(audio) - 8)
            struct.pack_into("<I", finalized, cursor + 4, len(audio) - cursor - 8)
            return bytes(finalized)
        cursor += 8 + size + (size & 1)
    return audio


def _read_audio(path_value):
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        return None
    path = Path(path_value).resolve(strict=True)
    if not path.is_relative_to(Path(get_astrbot_temp_path()).resolve()) or not path.is_file():
        return None
    with path.open("rb") as stream:
        audio = stream.read(MAX_VOICE_BYTES + 1)
    return _finalize_streamed_wav(audio) if 0 < len(audio) <= MAX_VOICE_BYTES else None


class NativeVoiceReply:
    def __init__(self, context):
        self.context = context

    async def render(self, task_context, text, *, provider_id="", timeout_seconds=30,
                     model_override="", voice_override=""):
        scope = task_context.scope
        kind = "GroupMessage" if scope.kind is ScopeKind.GROUP else "FriendMessage"
        umo = f"{scope.platform}:{kind}:{scope.session_id}"
        try:
            provider = None
            if provider_id:
                candidate = self.context.get_provider_by_id(provider_id)
                if candidate is not None and candidate in self.context.get_all_tts_providers():
                    provider = candidate
                else:
                    logger.warning("[CompanionV2] 指定 TTS Provider 未加载，回退默认语音服务 provider=%s", provider_id)
            if provider is None:
                getter = getattr(self.context, "get_using_tts_provider_async", None)
                if callable(getter):
                    provider = await getter(umo)
                else:
                    provider = self.context.get_using_tts_provider(umo)
            if provider is None:
                logger.warning("[CompanionV2] TTS Provider 不可用 reason=not_selected")
                return None
            if model_override or voice_override:
                provider = _provider_with_overrides(provider, model_override, voice_override)
            async def synthesize():
                method = getattr(provider, "get_audio", None) or getattr(provider, "synthesize_text", None)
                if not callable(method):
                    return None
                result = method(text.strip()) if inspect.iscoroutinefunction(method) else await asyncio.to_thread(method, text.strip())
                path = await result if inspect.isawaitable(result) else result
                if isinstance(path, (list, tuple)):
                    path = path[0] if path else None
                return await asyncio.to_thread(_read_audio, path)

            audio = await asyncio.wait_for(synthesize(), timeout=timeout_seconds)
            if audio is not None:
                return {"type": "record", "data": {"file": "base64://" + base64.b64encode(audio).decode("ascii")}}
        except Exception as error:
            logger.warning("V2 voice synthesis unavailable: %s", type(error).__name__)
        return None
