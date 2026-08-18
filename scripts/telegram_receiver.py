#!/usr/bin/env python3
"""Telegram 事件接入：对证生一，合格成议，主人裁决二与三。"""
import json
import itertools
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
from decipher_core import (
    append_jsonl, event_key, is_authorized, laws_by_id, merge_level, merge_one,
    parse_analysis, parse_proposal, pending_proposals, proposal_id,
    qualified_law_ids, qualifies, read_jsonl, write_text_atomic,
)

ROOT = Path(__file__).resolve().parent.parent
LLM_BASE = os.environ.get("DECIPHER_LLM_BASE", "https://api.deepseek.com/v1")

OFFSET_PATH = ROOT / "data" / "state" / "telegram_offset.json"
KEY_PATH = ROOT / "data" / "state" / "llm_key.txt"
MODEL_PATH = ROOT / "data" / "state" / "llm_model.txt"
LEDGER_PATH = ROOT / "data" / "state" / "ledger.jsonl"
LEVEL_PATHS = {"二": ROOT / "docs" / "二.md", "三": ROOT / "docs" / "三.md"}


def _owner_ids(raw: str | None = None) -> set[int]:
    raw = os.environ.get("DECIPHER_OWNER_CHAT_IDS", "") if raw is None else raw
    try:
        return {int(item.strip()) for item in raw.split(",") if item.strip()}
    except ValueError:
        return set()


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


# ──────────────────────────── LLM 调用 ────────────────────────────


def _llm(system: str, user: str, max_tokens: int = 6000) -> str:
    """调用兼容 chat completions；推理内容仅作空响应回退。"""
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
    """读取认知真源；知识增长后应由确定性检索缩小上下文。"""
    out = []
    for f in ("一.md", "二.md", "三.md", "道.md"):
        p = ROOT / "docs" / f
        body = p.read_text("utf-8").strip() if p.exists() else f"{f}: (空)"
        out.append(f"【{p.stem}】\n{body}")
    return "\n\n".join(out)


def _apply_analysis(raw: str, event: str, event_id: str,
                    one_path: Path = ONE_PATH,
                    ledger_path: Path = LEDGER_PATH) -> str:
    analysis = parse_analysis(raw, event, event_id)
    body = one_path.read_text("utf-8") if one_path.exists() else "# 一 · 律\n"
    records = []
    for law in analysis["laws"]:
        body, record = merge_one(body, law, event_id)
        records.append((law, record))
    write_text_atomic(one_path, body)
    append_jsonl(ledger_path, {
        "kind": "event", "id": event_id, "text": event,
        "aspects": analysis["aspects"],
    })
    lines = []
    actions = []
    reached = False
    for law, record in records:
        observation = {
            "kind": "observation",
            "id": f'{record["law_id"]}:{event_id}',
            "law_id": record["law_id"],
            "event_id": event_id,
            "similarity": record["similarity"],
        }
        append_jsonl(ledger_path, observation)
        related = [item for item in read_jsonl(ledger_path)
                   if item.get("kind") == "observation"
                   and item.get("law_id") == record["law_id"]]
        reached = reached or qualifies(
            [item["event_id"] for item in related],
            [item["similarity"] for item in related],
        )
        actions.append(record["action"])
        lines.append(
            f'- {law["statement"].rstrip("。.")}。'
            f'（相：{", ".join(law["aspects"])}；据：{event_id}）'
        )
    step = "、".join(dict.fromkeys(actions)) if actions else "无律可录"
    one = "\n".join(lines) if lines else "证据不足"
    status = "已达升维观察门槛，待形成同层规律群" if reached else "未升维（累计不足）"
    return (
        f"【归纳步骤】\n{event_id} → 五窗判定 → {step}\n"
        f"【一】\n{one}\n"
        f"【道判】\n相：{', '.join(analysis['aspects'])}；"
        f"{analysis['judgment']}\n{status}"
    )


def _analyze_event(query: str, event_id: str) -> str:
    """事件经五窗观察；模型提候选，程序对证并决定是否写入。"""
    laws = _read_laws()
    system = (
        "你是 Decipher：只观察、提炼、建议，不裁决。事件必须经过性本、调欲、"
        "修心、处世、超越五窗。只输出一个 JSON 对象，禁止 Markdown。结构为："
        '{"aspects":["处世"],"judgment":"一句判定",'
        '"laws":[{"statement":"一句极简律文","aspects":["处世"],'
        '"evidence":"事件中的连续原文"}]}。'
        "相只能取性本、调欲、修心、处世、超越；evidence 必须逐字来自事件。"
        "证据不足时 laws 为空数组，绝不补造事实。"
    )
    user = (
        f"【主人描述的事件】\n{query}\n\n"
        f"【现有认知】\n{laws}\n\n只返回 JSON 候选。"
    )
    try:
        raw = _llm(system, user, max_tokens=4000)
        result = _apply_analysis(raw, query, event_id)
        try:
            proposal = _maybe_propose("二", "一", ONE_PATH, LEDGER_PATH)
            if proposal:
                result += f'\n待主人裁决：{proposal["id"]}'
        except Exception:
            logger.error("二层提案失败:\n%s", traceback.format_exc())
        return result
    except ValueError as exc:
        logger.warning("候选拒绝: %s: %s", event_id, exc)
        return f"【归纳步骤】\n{event_id} → 候选拒绝\n未升维（证据或格式不成立）"
    except Exception:
        logger.error("事件分析失败: %s", event_id)
        raise


# ──────────────────────────── Handler 注册表 ────────────────────────────


def _cmd_start(chat_id: int, _text: str):
    send_message(chat_id, "Decipher 在线。可描述事件，我经五窗对证归纳。")


def _cmd_help(chat_id: int, _text: str):
    send_message(chat_id, "/ping 在线检查\n/laws 查看认知\n/pending 待裁决\n"
                 "/approve <键> 批准\n/reject <键> 驳回\n\n直接发事件，我对证归纳。")


def _cmd_laws(chat_id: int, _text: str):
    body = _read_laws()
    for seg in split_text(body):
        send_markdown(chat_id, seg)


def _decide(proposal_id: str, status: str,
            ledger_path: Path = LEDGER_PATH,
            level_paths: dict[str, Path] = LEVEL_PATHS) -> str:
    records = read_jsonl(ledger_path)
    proposal = next((item for item in pending_proposals(records)
                     if item.get("id") == proposal_id), None)
    if proposal is None:
        return "未找到待裁决项。"
    if status == "approved":
        path = level_paths[proposal["level"]]
        body = path.read_text("utf-8") if path.exists() else f'# {proposal["level"]}\n'
        body, record = merge_level(
            body, proposal, proposal["source_ids"], proposal["level"],
        )
        write_text_atomic(path, body)
        append_jsonl(ledger_path, {
            "kind": "observation",
            "id": f'{record["law_id"]}:{proposal_id}',
            "law_id": record["law_id"],
            "event_id": proposal_id,
            "similarity": record["similarity"],
        })
    append_jsonl(ledger_path, {
        "kind": "decision",
        "id": f"decision:{proposal_id}",
        "proposal_id": proposal_id,
        "status": status,
    })
    return f"{proposal_id} 已{'批准' if status == 'approved' else '驳回'}。"


def _maybe_propose(level: str, source_level: str, source_path: Path,
                   ledger_path: Path = LEDGER_PATH) -> dict | None:
    records = read_jsonl(ledger_path)
    qualified = qualified_law_ids(records, source_level)
    if len(qualified) < 3:
        return None
    laws = laws_by_id(source_path.read_text("utf-8"), source_level)
    existing = {(item.get("level"), tuple(sorted(item.get("source_ids", []))))
                for item in records if item.get("kind") == "proposal"}
    sources = next((list(group) for group in itertools.combinations(qualified, 3)
                    if (level, tuple(sorted(group))) not in existing
                    and all(item in laws for item in group)), None)
    if sources is None:
        return None
    source_text = "\n".join(f'{item}: {laws[item]["statement"]}' for item in sources)
    raw = _llm(
        "你是 Decipher。将三条已对证的同层规律归纳为一条更普遍的律。"
        "只返回 JSON："
        '{"statement":"一句极简律文","aspects":["处世"],'
        '"source_ids":["来源键"]}。不得改变来源键，不得增加事实。',
        f"目标层：{level}\n来源：\n{source_text}",
        max_tokens=2000,
    )
    proposal = parse_proposal(raw, level, sources)
    proposal.update({
        "kind": "proposal",
        "id": proposal_id(level, sources),
    })
    append_jsonl(ledger_path, proposal)
    return proposal


def _cmd_pending(chat_id: int, _text: str):
    items = pending_proposals(read_jsonl(LEDGER_PATH))
    if not items:
        send_message(chat_id, "无待裁决项。")
        return
    text = "\n".join(
        f'{item["id"]} [{item["level"]}] {item["statement"]}' for item in items
    )
    send_message(chat_id, text)


def _decision_command(chat_id: int, text: str, status: str):
    parts = text.split()
    if len(parts) != 2:
        send_message(chat_id, "格式：/approve <提案键>" if status == "approved"
                     else "格式：/reject <提案键>")
        return
    send_message(chat_id, _decide(parts[1], status))


def _cmd_approve(chat_id: int, text: str):
    _decision_command(chat_id, text, "approved")
    try:
        proposal = _maybe_propose("三", "二", LEVEL_PATHS["二"], LEDGER_PATH)
        if proposal:
            send_message(chat_id, f'新待裁决项：{proposal["id"]}')
    except Exception:
        logger.error("三层提案失败:\n%s", traceback.format_exc())


def _cmd_reject(chat_id: int, text: str):
    _decision_command(chat_id, text, "rejected")


HANDLERS = {
    "/start": _cmd_start,
    "/ping": _cmd_start,
    "/help": _cmd_help,
    "/laws": _cmd_laws,
    "/pending": _cmd_pending,
    "/approve": _cmd_approve,
    "/reject": _cmd_reject,
}


def _reply_stream(chat_id: int, text: str, event_id: str = ""):
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
        result = _analyze_event(text, event_id)
        acc = result
        outcome = True
    except Exception:
        logger.error("analyze error:\n%s", traceback.format_exc())
        acc = "（分析暂时失败，将保留事件重试。）"
        outcome = None
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
        return outcome
    for seg in split_text(acc):
        send_markdown(chat_id, seg)
    return outcome


def _handle(update_id: int, chat_id: int, text: str,
            owners: set[int] | None = None) -> bool:
    owners = _owner_ids() if owners is None else owners
    if not is_authorized(chat_id, owners):
        send_message(chat_id, "无权访问。")
        return False
    text = (text or "").strip()
    if not text:
        return False
    cmd = text.split(" ", 1)[0].lower()
    fn = HANDLERS.get(cmd)
    if fn:
        fn(chat_id, text)
        return True
    event_id = event_key(update_id)
    logger.info("Decipher 事件: %s", event_id)
    return _reply_stream(chat_id, text, event_id)


def _load_offset() -> int:
    try:
        if OFFSET_PATH.exists():
            return int(OFFSET_PATH.read_text().strip() or 0)
    except Exception:
        pass
    return 0


def _save_offset(offset: int):
    write_text_atomic(OFFSET_PATH, str(offset))


def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        logger.error("TELEGRAM_BOT_TOKEN 未配置")
        return 1
    if not _owner_ids():
        logger.error("DECIPHER_OWNER_CHAT_IDS 未配置或无效")
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
            next_offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if chat_id is None or not text.strip():
                offset = next_offset
                _save_offset(offset)
                continue
            try:
                outcome = _handle(u["update_id"], chat_id, text)
            except Exception:
                logger.error("handle error:\n%s", traceback.format_exc())
                outcome = None
            if outcome is None:
                time.sleep(5.0)
                break
            offset = next_offset
            _save_offset(offset)


if __name__ == "__main__":
    sys.exit(main())
