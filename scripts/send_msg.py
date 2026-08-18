#!/usr/bin/env python3
"""Decipher/scripts/send_msg.py — Telegram Bot API 封装（标准库 urllib，零依赖）。

Decipher 只读不写，限流状态用进程内变量代替。
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

MAX_LEN = 4096

_rate_state = {"interval": 1.0}


def _api_url(method: str, token: str | None = None) -> str:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 未配置")
    return f"https://api.telegram.org/bot{token}/{method}"


def _post(method: str, payload: dict, token: str | None = None,
          timeout: int = 15) -> dict:
    url = _api_url(method, token)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            out = json.loads(body)
        except Exception:
            raise
        if isinstance(out, dict):
            out.setdefault("ok", False)
            params = out.get("parameters") or {}
            if params.get("retry_after"):
                _rate_state["interval"] = float(params["retry_after"])
            return out
        raise


def rate_interval(default: float = 1.0) -> float:
    """当前请求间隔（秒）：429 时从 retry_after 更新，成功调用回落基线。"""
    return _rate_state.get("interval") or default


def _md_failed(out: dict) -> bool:
    return not out.get("ok", False) and "can't parse entities" in json.dumps(out.get("description", ""))


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    """按不超过 limit 分片（尽量在换行处断）。"""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
    return parts


def send_message(chat_id: int, text: str, parse_mode: str = "",
                 reply_to_message_id: int | None = None) -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return _post("sendMessage", payload)


def edit_message(chat_id: int, message_id: int, text: str,
                 parse_mode: str = "") -> dict:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _post("editMessageText", payload)


def send_markdown(chat_id: int, text: str, reply_to_message_id: int | None = None) -> dict:
    out = send_message(chat_id, text, parse_mode="Markdown",
                       reply_to_message_id=reply_to_message_id)
    if _md_failed(out):
        return send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
    return out


def edit_markdown(chat_id: int, message_id: int, text: str) -> dict:
    out = edit_message(chat_id, message_id, text, parse_mode="Markdown")
    if _md_failed(out):
        return edit_message(chat_id, message_id, text)
    return out


def get_updates(offset: int | None = None, timeout: int = 25) -> dict:
    payload = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    return _post("getUpdates", payload, timeout=timeout + 5)