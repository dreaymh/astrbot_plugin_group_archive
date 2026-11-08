"""OneBot 调用与消息段解析。"""

from __future__ import annotations

from typing import Any

import astrbot.api.message_components as Comp


def unwrap(value: Any) -> Any:
    """剥掉 OneBot 响应外层 data。"""
    for _ in range(4):
        if not isinstance(value, dict) or "data" not in value:
            return value
        if any(k in value for k in ("message", "content", "messages", "nodes")):
            return value
        value = value["data"]
    return value


def callers(event: Any) -> list[Any]:
    bot = getattr(event, "bot", None)
    return [
        c
        for c in (
            getattr(bot, "call_action", None),
            getattr(getattr(bot, "api", None), "call_action", None),
        )
        if callable(c)
    ]


async def call_action(event: Any, action: str, **params: Any) -> Any:
    """读取类接口，失败时换入口重试。"""
    last: Exception | None = None
    for caller in callers(event):
        try:
            return unwrap(await caller(action, **params))
        except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一
            last = exc
    if last:
        raise last
    raise RuntimeError("当前 OneBot 适配器未提供 call_action")


def history_from_bot(bot: Any):
    """返回一个 (group_id, message_id, count) -> list 的异步翻页函数。"""
    fn = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
    if not callable(fn):
        return None

    async def _fetch(group_id: int, message_id: int, count: int = 200) -> list[dict]:
        data = unwrap(
            await fn(
                "get_group_msg_history",
                group_id=group_id,
                message_id=message_id,
                count=count,
                reverse_order=True,
            )
        )
        if isinstance(data, dict):
            return list(data.get("messages") or [])
        if isinstance(data, list):
            return data
        return []

    return _fetch


def plain_text(event: Any) -> str:
    """拼出消息中的纯文本；引用段本身不含正文，直接跳过。"""
    try:
        segs = event.get_messages() or []
    except Exception:  # noqa: BLE001
        segs = []
    return "".join(seg.text or "" for seg in segs if isinstance(seg, Comp.Plain)).strip()


def find_reply(event: Any) -> Any | None:
    try:
        segs = event.get_messages() or []
    except Exception:  # noqa: BLE001
        return None
    for seg in segs:
        if isinstance(seg, Comp.Reply):
            return seg
    return None


def segments_text(segments: Any) -> str:
    """从 OneBot 消息段数组拼纯文本。"""
    if not isinstance(segments, list):
        return ""
    out: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            out.append(str(data.get("text") or ""))
        elif kind == "image":
            out.append("[图片]")
        elif kind in ("face", "mface"):
            out.append("[表情]")
    return "".join(out).strip()


async def quoted_text(event: Any, reply: Any) -> str:
    """取被引用消息的正文，优先本地字段，必要时回查接口。"""
    text = str(getattr(reply, "message_str", "") or "").strip()
    if text:
        return text
    chain = getattr(reply, "chain", None) or []
    if chain:
        joined = "".join(getattr(c, "text", "") or "" for c in chain if isinstance(c, Comp.Plain))
        if joined.strip():
            return joined.strip()
    mid = getattr(reply, "id", None)
    if mid in (None, "", 0):
        return ""
    try:
        data = await call_action(event, "get_msg", message_id=mid)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(data, dict):
        return segments_text(data.get("message"))
    return ""
