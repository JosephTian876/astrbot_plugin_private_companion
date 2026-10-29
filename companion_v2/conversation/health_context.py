"""Bounded health material for a private foreground model request.

The source is the user's explicitly authorized device telemetry.  This module
only projects existing measurements; it never diagnoses, predicts or fills
missing readings, and it never carries health material into group chats.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math


SNAPSHOT_VERSION = "conversation.health.v1"
MAX_MEASUREMENTS = 12
MAX_TEXT = 80
MAX_SUMMARY = 600

HEALTH_INSTRUCTION = (
    "health_context 只来自当前私聊对象明确授权的健康数据源（手机上报或已绑定的华为运动健康账号），是用户本人允许读取的最近摘要，"
    "不是医疗诊断、实时生命体征或完整病史，也不能拿来推断对方此刻的睡眠、情绪、疾病或行为。"
    "仅在当前话题自然相关时使用：用户问身体、睡眠、心率、步数或运动时可据此回答；"
    "旧读数要保留测量时间与时效，不把过期数据说成当前状态。"
    "不要主动盘问、监督饮食作息、制造健康焦虑，也不根据一次读数下诊断或给医疗建议；"
    "没有数据、授权撤回或读数不足时直接说明不知道，不猜测、不补造。"
    "群聊和其他窗口不得使用这份私聊健康材料。"
)


def _text(value, limit=MAX_TEXT):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _number(value):
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return value


def _iso_epoch(value):
    number = _number(value)
    if number is None or number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _measurement(item, observed_at):
    if type(item) is not dict:
        return None
    name = _text(item.get("type"), 40).lower()
    label = _text(item.get("label"), 48)
    value = _number(item.get("value"))
    unit = _text(item.get("unit"), 16)
    if (not name and not label) or value is None or not unit:
        return None
    sample_at = _number(item.get("captured_at"))
    if sample_at is not None and sample_at - observed_at > 10 * 60:
        return None
    measured_at = _iso_epoch(sample_at)
    return {
        "type": name or label,
        "label": label or name,
        "value": round(value, 3),
        "unit": unit,
        **({"captured_at": measured_at} if measured_at else {}),
    }


def project_health_context(snapshot, *, observed_at):
    """Project one bounded, non-diagnostic health summary.

    Returns ``None`` when there is no usable, authorized reading.  The caller
    owns the consent decision; this function only validates the data shape.
    """
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ValueError("health context requires a timezone-aware observation time")
    if type(snapshot) is not dict:
        return None
    telemetry = snapshot.get("telemetry")
    if type(telemetry) is not dict or telemetry.get("available") is not True:
        return None
    timestamp = observed_at.timestamp()
    measurements = []
    seen = set()
    for raw in telemetry.get("measurements") or ():
        item = _measurement(raw, timestamp)
        if item is None:
            continue
        key = item["type"]
        if key in seen:
            continue
        seen.add(key)
        measurements.append(item)
        if len(measurements) >= MAX_MEASUREMENTS:
            break
    activity_raw = telemetry.get("activity") if isinstance(telemetry.get("activity"), dict) else {}
    activity = _text(activity_raw.get("label") or activity_raw.get("state"), 80)
    if not measurements and not activity:
        return None
    parts = [f"{item['label']} {item['value']:g} {item['unit']}" for item in measurements]
    if activity:
        parts.append(f"当前活动：{activity}")
    age = _number(telemetry.get("age_seconds"))
    return {
        "version": SNAPSHOT_VERSION,
        "basis": "user_authorized_device_health",
        "privacy": "private_only",
        "source": "mobile_health_telemetry",
        "captured_at": _iso_epoch(telemetry.get("captured_at")),
        "age_seconds": max(0, round(age)) if age is not None else None,
        "measurements": measurements,
        "activity": activity,
        "summary": "；".join(parts)[:MAX_SUMMARY],
    }


__all__ = ["HEALTH_INSTRUCTION", "MAX_MEASUREMENTS", "SNAPSHOT_VERSION", "project_health_context"]
