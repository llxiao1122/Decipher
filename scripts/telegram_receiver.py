#!/usr/bin/env python3
"""Decipher Telegram 长轮询接收器 —— 解码者接入线。

职责:
  - 主人通过 TG 发来事件/疑点，Decipher 读取四层 docs 归纳分析
  - 新规律自动纳入 docs/一.md（主人裁决：录入事件直接入库，无需确认）
  - 输出按链呈现：取材（证据键）→ 一（规律）→ 二（统）→ 三（势）→ 道

LLM:
  - 默认 DeepSeek v4 pro（thinking 开启），OpenAI 兼容 /chat/completions
  - 模型/Key 可运行时覆盖：data/state/llm_model.txt、llm_key.txt
  - 环境变量可覆盖：DECIPHER_LLM_BASE / DECIPHER_LLM_MODEL / DEEPSEEK_API_KEY

启动:
  TELEGRAM_BOT_TOKEN=xxx DECIPHER_LLM_BASE=https://api.deepseek.com/v1 \
  python3 scripts/telegram_receiver.py
"""
import json
import logging
import os
import re
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

OFFSET_PATH = ROOT / "data" / "state" / "telegram_offset.json"
KEY_PATH = ROOT / "data" / "state" / "llm_key.txt"
MODEL_PATH = ROOT / "data" / "state" / "llm_model.txt"


def _read_cfg(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text("utf-8").strip()
    except Exception:
        pass
    return ""


def _llm_model() -> str:
    """模型优先取 llm_model.txt（主人指定，可运行时更新），回退环境变量/默认。"""
    return _read_cfg(MODEL_PATH) or os.environ.get("DECIPHER_LLM_MODEL", "deepseek-v4-flash")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("decipher_tg")


# ──────────────────────────── 只读知识层 ────────────────────────────


def _read_laws() -> str:
    """读四层 docs（唯一可写文件，此处只读）。"""
    out = []
    for f in ("一.md", "二.md", "三.md", "道.md"):
        p = ROOT / "docs" / f
        body = p.read_text("utf-8").strip() if p.exists() else f"{f}: (空)"
        out.append(f"【{p.stem}】\n{body[:6000]}")
    return "\n\n".join(out)


# ──────────────────────────── LLM 调用 ────────────────────────────


def _llm(system: str, user: str, max_tokens: int = 6000) -> str:
    """OpenAI 兼容 chat completions（模型取 llm_model.txt，默认 DeepSeek v4 pro）。

    pro 为推理模型，thinking 开启：content 可能为空而答案在 reasoning_content，
    故回退取 reasoning_content 保证有输出。"""
    headers = {"Content-Type": "application/json"}
    # key 优先取 llm_key.txt（主人指定，可运行时更新），回退环境变量
    key = ""
    try:
        if KEY_PATH.exists():
            key = KEY_PATH.read_text("utf-8").strip()
    except Exception:
        pass
    if not key:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": _llm_model(),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "reasoning_effort": "max",
        "thinking": {"type": "enabled"},
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


# ──────────────────────────── 归纳执行（decipher-dao） ────────────────────────────

ONE_PATH = ROOT / "docs" / "一.md"


def _read_laws() -> str:
    """读四层 docs（唯一可写文件，此处只读）。"""
    out = []
    for f in ("一.md", "二.md", "三.md", "道.md"):
        p = ROOT / "docs" / f
        body = p.read_text("utf-8").strip() if p.exists() else f"{f}: (空)"
        out.append(f"【{p.stem}】\n{body[:6000]}")
    return "\n\n".join(out)


def _text_sim(a: str, b: str) -> float:
    """字符级相似度（去标点后字符集合 Jaccard；短句包含视为 1.0）。"""
    import re as _re
    ca = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", a)
    cb = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", b)
    if not ca or not cb:
        return 0.0
    sa, sb = set(ca), set(cb)
    j = len(sa & sb) / len(sa | sb)
    if j >= 0.9:
        return j
    if (len(ca) >= 4 and ca in cb) or (len(cb) >= 4 and cb in ca):
        return 1.0
    return j


def _ingest_new_ones(text: str) -> list:
    """解析【新规律入库】块，与一.md 现有律去重后自动编号追加，返回实际写入的律。"""
    m = re.search(r"【新规律入库】\s*(.*?)(?=\n【|\Z)", text, re.S)
    if not m:
        return []
    block = m.group(1).strip()
    if not block or block in ("无", "无。", "无新规律"):
        return []
    body = ONE_PATH.read_text("utf-8") if ONE_PATH.exists() else "# 一 · 律\n\n"
    existing = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
    saved = []
    for line in block.splitlines():
        line = line.strip().lstrip("-•").strip()
        if not line or line.startswith("【") or "据：" not in line:
            continue
        if any(_text_sim(line, re.sub(r"^\s*\d+\.\s*", "", e)) >= 0.9
               for e in existing):
            continue
        saved.append(line)
    if saved:
        nums = [int(m2.group(1)) for l in existing
                if (m2 := re.match(r"\s*(\d+)\.", l))]
        n = (max(nums) if nums else 0) + 1
        with ONE_PATH.open("a", encoding="utf-8") as f:
            for s in saved:
                f.write(f"{n}. {s}\n")
                n += 1
        logger.info("📥 新规律已入库一.md：%s", saved)
    return saved


def _analyze_event(query: str) -> str:
    """主人 TG 描述的事件 → 对照四层律升维归纳 → 输出规律与归纳步骤。"""
    laws = _read_laws()
    system = (
        "你是 Decipher，解码者：把主人描述的事件之密文，解为规律之明文。"
        "只洞察与建议，不裁决不执行——裁决权在主人；证据不足时如实说'不知道'。"
        "归纳升维（二次升维）：新事件先与现有律对照——被涵盖则不新增只补强；"
        "可涵盖旧律则合并（总量不增）；冲突则升维统一。\n\n"
        "输出要求（全文简体中文，仅输出变动部分，禁止铺垫）：\n"
        "【归纳步骤】2-3 行：事件 → 对照 → 升维动作（新增/补强/合并/冲突升维）\n"
        "【一】规律：...\n"
        "【二】天理：仅升维时输出；未升维则不输出该段\n"
        "【三】势：仅升维时输出；未升维则不输出该段\n"
        "【道】仅升维时输出，且标注'待主人裁决'；未升维则不输出该段\n"
        "【新规律入库】若识别出可纳入一的新规律（未被现有'一'库涵盖），"
        "此处每行输出一条：- 律文（据：证据键）；无新规律则写'无'。\n"
        "若事件不足以支撑任何规律，如实说'证据不足'，不臆造。"
    )
    user = (
        f"【主人描述的事件】\n{query}\n\n"
        f"【现有四层律】\n{laws}\n\n"
        f"请按上述要求归纳升维，仅输出变动部分：归纳步骤、一，以及触发升维时的二/三/道、新规律入库。"
    )
    try:
        result = _llm(system, user, max_tokens=8000)
    except Exception:
        return "（LLM 不可达或出错，未能完成归纳。）"
    saved = _ingest_new_ones(result)
    if saved:
        result += "\n\n✅ 已自动纳入一：\n" + "\n".join(f"- {s}" for s in saved)
    elif "【新规律入库】" not in result:
        result += "\n\n（未检出新规律入库块，未写入一.md）"
    return result


# ──────────────────────────── Handler 注册表 ────────────────────────────


def _cmd_start(chat_id: int, _text: str):
    send_message(chat_id, "Decipher 在线。可描述事件/疑点，我对照四层律归纳升维。")


def _cmd_help(chat_id: int, _text: str):
    send_message(chat_id, "/ping 在线检查\n/help 本帮助\n/laws 查看四层律\n\n直接发消息=描述事件，我归纳升维后回复规律与步骤。")


def _cmd_laws(chat_id: int, _text: str):
    body = _read_laws()
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
                offset, LLM_BASE, _llm_model())

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