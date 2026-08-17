#!/usr/bin/env python3
"""Decipher Telegram 长轮询接收器 —— 解码者接入线。

职责（严守 Decipher 只读纪律）:
  - 主人通过 TG 发来事件/疑点，Decipher 读取三大律 docs 归纳分析
  - 只读 Cipher data/mem，不做任何写入，不改 Cipher 状态
  - 输出按链呈现：取材（证据键）→ 一（规律）→ 二（天理）→ 道（一以贯之）

LLM:
  - 默认 Ollama qwen2.5:14b（OpenAI 兼容 /v1/chat/completions）
  - 环境变量可覆盖：DECIPHER_LLM_BASE / DECIPHER_LLM_MODEL

启动:
  TELEGRAM_BOT_TOKEN=xxx DECIPHER_LLM_BASE=http://100.118.99.51:11434/v1 \
  python3 scripts/telegram_receiver.py
"""
import json
import logging
import os
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from send_msg import (
    get_updates, send_message, edit_message, edit_markdown, send_markdown,
    MAX_LEN, split_text,
)

ROOT = Path(__file__).resolve().parent.parent
LLM_BASE = os.environ.get("DECIPHER_LLM_BASE", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("DECIPHER_LLM_MODEL", "deepseek-v4-flash")

OFFSET_PATH = ROOT / "data" / "state" / "telegram_offset.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("decipher_tg")


# ──────────────────────────── 只读知识层 ────────────────────────────


def _read_three_laws() -> str:
    """读三大律 docs（唯一可写文件，此处只读）。"""
    out = []
    for f in ("一.md", "二.md", "道.md"):
        p = ROOT / "docs" / f
        body = p.read_text("utf-8").strip() if p.exists() else f"{f}: (空)"
        out.append(f"【{p.stem}】\n{body[:6000]}")
    return "\n\n".join(out)


# ──────────────────────────── LLM 调用 ────────────────────────────


def _llm(system: str, user: str, max_tokens: int = 6000) -> str:
    """OpenAI 兼容 chat completions（默认 DeepSeek v4 flash）。

    flash 为推理模型：content 可能为空而答案在 reasoning_content，故回退
    取 reasoning_content 保证有输出。"""
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.loads(resp.read())
        msg = out["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            content = (msg.get("reasoning_content") or "").strip()
        return content
    except HTTPError as e:
        logger.error("LLM HTTPError %s: %s", e.code, e.read().decode("utf-8", "replace")[:300])
        raise
    except Exception:
        logger.error("LLM error:\n%s", traceback.format_exc())
        raise


# ──────────────────────────── 归纳执行（decipher-dao，只读） ────────────────────────────


def _analyze_event(query: str) -> str:
    """主人 TG 描述的事件 → 对照三大律升维归纳 → 只输出规律与归纳步骤。"""
    laws = _read_three_laws()
    system = (
        "你是 Decipher，解码者：把主人描述的事件之密文，解为规律之明文。"
        "只洞察与建议，不裁决不执行——裁决权在主人；证据不足时如实说'不知道'。"
        "归纳升维（二次升维）：新事件先与现有三大律对照——被涵盖则不新增只补强；"
        "可涵盖旧律则合并（总量不增）；冲突则升维统一。\n\n"
        "输出要求（全文简体中文，仅以下四段，禁止铺垫）：\n"
        "【归纳步骤】2-3 行：事件 → 对照 → 升维动作（新增/补强/合并/冲突升维）\n"
        "【规律】一：...\n"
        "【天理】二：...\n"
        "【道】...\n"
        "若事件不足以支撑任何规律，如实说'证据不足'，不臆造。"
    )
    user = (
        f"【主人描述的事件】\n{query}\n\n"
        f"【现有三大律】\n{laws}\n\n"
        f"请按上述要求归纳升维，仅输出四段：归纳步骤、规律、天理、道。"
    )
    try:
        return _llm(system, user, max_tokens=8000)
    except Exception:
        return "（LLM 不可达或出错，未能完成归纳。）"


# ──────────────────────────── Handler 注册表 ────────────────────────────


def _cmd_start(chat_id: int, _text: str):
    send_message(chat_id, "Decipher 在线。可描述事件/疑点，我对照三大律归纳升维。")


def _cmd_help(chat_id: int, _text: str):
    send_message(chat_id, "/ping 在线检查\n/help 本帮助\n/laws 查看三大律\n\n直接发消息=描述事件，我归纳升维后回复规律与步骤。")


def _cmd_laws(chat_id: int, _text: str):
    body = _read_three_laws()
    for seg in split_text(body):
        send_markdown(chat_id, seg)


HANDLERS = {
    "/start": _cmd_start,
    "/ping": _cmd_start,
    "/help": _cmd_help,
    "/laws": _cmd_laws,
}


def _reply_stream(chat_id: int, text: str):
    """占位 → 生成 → 定格（复用 TG edit 渐进渲染）。"""
    mid = None
    acc = ""
    try:
        init = send_message(chat_id, "… Decipher 解码中")
        if init.get("ok"):
            mid = init["result"]["message_id"]
    except Exception:
        pass
    try:
        result = _analyze_event(text)
        acc = result
    except Exception:
        logger.error("analyze error:\n%s", traceback.format_exc())
        acc = "（分析出错）"
    if mid:
        try:
            edit_markdown(chat_id, mid, acc[:MAX_LEN])
        except Exception:
            try:
                edit_message(chat_id, mid, acc[:MAX_LEN])
            except Exception:
                pass
        rest = split_text(acc)[1:]
        for seg in rest:
            send_markdown(chat_id, seg)
        return
    for seg in split_text(acc):
        send_markdown(chat_id, seg)


def _handle(chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return
    cmd = text.split(" ", 1)[0].lower()
    fn = HANDLERS.get(cmd)
    if fn:
        fn(chat_id, text)
        return
    logger.info("📨 Decipher 消息: [%s] %s", chat_id, text[:80])
    _reply_stream(chat_id, text)


def _load_offset() -> int:
    try:
        if OFFSET_PATH.exists():
            return int(OFFSET_PATH.read_text().strip() or 0)
    except Exception:
        pass
    return 0


def _save_offset(offset: int):
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset))


def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        logger.error("TELEGRAM_BOT_TOKEN 未配置")
        return 1

    offset = _load_offset()
    logger.info("🚀 Decipher Telegram Receiver 启动（offset=%s，LLM=%s/%s）",
                offset, LLM_BASE, LLM_MODEL)

    while True:
        try:
            updates = get_updates(offset=offset)
        except Exception:
            logger.warning("getUpdates 失败，重试")
            time.sleep(5.0)
            continue

        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if chat_id is None or not text.strip():
                continue
            try:
                _handle(chat_id, text)
            except Exception:
                logger.error("handle error:\n%s", traceback.format_exc())
            _save_offset(offset)


if __name__ == "__main__":
    sys.exit(main())