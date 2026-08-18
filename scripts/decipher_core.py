"""Decipher 的纯领域原语。"""
import json
import hashlib
import os
import re
import tempfile
from pathlib import Path


ASPECTS = {"性本", "调欲", "修心", "处世", "超越"}


def is_authorized(chat_id: int, owners: set[int]) -> bool:
    return bool(owners) and chat_id in owners


def event_key(update_id: int) -> str:
    return f"evt_tg_{int(update_id)}"


def _strings(value, name: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
            isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{name} 必须是非空字符串数组")
    return [item.strip() for item in value]


def parse_analysis(raw: str, event: str, event_id: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型输出不是 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("模型输出必须是对象")
    aspects = _strings(data.get("aspects"), "aspects")
    if not set(aspects) <= ASPECTS:
        raise ValueError("存在未知相")
    judgment = data.get("judgment")
    laws = data.get("laws")
    if not isinstance(judgment, str) or not judgment.strip():
        raise ValueError("judgment 缺失")
    if not isinstance(laws, list):
        raise ValueError("laws 必须是数组")
    parsed = []
    for law in laws:
        if not isinstance(law, dict):
            raise ValueError("规律必须是对象")
        statement = law.get("statement")
        evidence = law.get("evidence")
        law_aspects = _strings(law.get("aspects"), "law.aspects")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError("规律缺少律文")
        if not isinstance(evidence, str) or not evidence.strip() or evidence not in event:
            raise ValueError("证据必须逐字来自事件")
        if not set(law_aspects) <= ASPECTS:
            raise ValueError("规律存在未知相")
        parsed.append({
            "statement": statement.strip(),
            "aspects": law_aspects,
            "evidence": evidence.strip(),
            "evidence_ids": [event_id],
        })
    return {
        "event_id": event_id,
        "aspects": aspects,
        "judgment": judgment.strip(),
        "laws": parsed,
    }


def _normalized(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text).lower()


def text_similarity(left: str, right: str) -> float:
    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return 0.0
    if a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a)):
        return 1.0
    aa = {a[i:i + 2] for i in range(max(1, len(a) - 1))}
    bb = {b[i:i + 2] for i in range(max(1, len(b) - 1))}
    return len(aa & bb) / len(aa | bb)


def qualifies(evidence_ids: list[str], similarities: list[float],
              threshold: float = 0.9) -> bool:
    return (
        len(set(evidence_ids)) >= 3
        and len(evidence_ids) == len(similarities)
        and all(score >= threshold for score in similarities)
    )


_LAW = re.compile(
    r"^(\d+)\.\s*(.+?)（相：([^；）]+)；据：([^）]+)）\s*$"
)
_LEGACY_LAW = re.compile(r"^(\d+)\.\s*(.+?)（据：([^）]+)）\s*$")


def _law_lines(body: str) -> list[dict]:
    laws = []
    for pos, line in enumerate(body.splitlines()):
        match = _LAW.match(line)
        if match:
            number, statement, aspects, evidence = match.groups()
            laws.append({
                "pos": pos,
                "number": int(number),
                "statement": statement.rstrip("。."),
                "aspects": [item.strip() for item in aspects.split(",")],
                "evidence_ids": [item.strip() for item in evidence.split(",")],
            })
            continue
        match = _LEGACY_LAW.match(line)
        if match:
            number, statement, evidence = match.groups()
            laws.append({
                "pos": pos,
                "number": int(number),
                "statement": statement.rstrip("。."),
                "aspects": [],
                "evidence_ids": [evidence.strip()],
            })
    return laws


def _render_law(law: dict) -> str:
    aspects = ", ".join(law["aspects"])
    evidence = ", ".join(law["evidence_ids"])
    return f'{law["number"]}. {law["statement"]}。（相：{aspects}；据：{evidence}）'


def merge_one(body: str, candidate: dict, event_id: str,
              threshold: float = 0.9) -> tuple[str, dict]:
    out, record = merge_level(
        body, candidate, [event_id], "一", threshold=threshold,
    )
    record["event_id"] = event_id
    return out, record


def merge_level(body: str, candidate: dict, evidence_ids: list[str],
                level: str, threshold: float = 0.9) -> tuple[str, dict]:
    laws = _law_lines(body)
    statement = candidate["statement"].strip().rstrip("。.")
    aspects = list(dict.fromkeys(candidate["aspects"]))
    matches = [(text_similarity(statement, law["statement"]), law) for law in laws]
    similarity, law = max(matches, default=(0.0, None), key=lambda item: item[0])
    if law is not None and similarity >= threshold:
        record = {
            "law_id": f'{level}_{law["number"]}',
            "similarity": round(similarity, 4),
        }
        new_evidence = [item for item in evidence_ids
                        if item not in law["evidence_ids"]]
        if not new_evidence:
            record["action"] = "duplicate"
            return body, record
        law["evidence_ids"].extend(new_evidence)
        law["aspects"] = list(dict.fromkeys(law["aspects"] + aspects))
        lines = body.splitlines()
        lines[law["pos"]] = _render_law(law)
        record["action"] = "reinforce"
        return "\n".join(lines) + ("\n" if body.endswith("\n") else ""), record
    number = max((item["number"] for item in laws), default=0) + 1
    law = {
        "number": number,
        "statement": statement,
        "aspects": aspects,
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
    }
    out = body.rstrip() + "\n" + _render_law(law) + "\n"
    return out, {
        "action": "new",
        "law_id": f"{level}_{number}",
        "similarity": 1.0,
    }


def parse_proposal(raw: str, level: str, source_ids: list[str]) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("升维候选不是 JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("statement"), str):
        raise ValueError("升维候选缺少律文")
    aspects = _strings(data.get("aspects"), "aspects")
    cited = _strings(data.get("source_ids"), "source_ids")
    if not set(aspects) <= ASPECTS or set(cited) != set(source_ids):
        raise ValueError("升维候选来源不成立")
    return {
        "level": level,
        "statement": data["statement"].strip(),
        "aspects": aspects,
        "source_ids": source_ids,
    }


def pending_proposals(records: list[dict]) -> list[dict]:
    decided = {item.get("proposal_id") for item in records
               if item.get("kind") == "decision"}
    return [item for item in records
            if item.get("kind") == "proposal" and item.get("id") not in decided]


def qualified_law_ids(records: list[dict], level: str) -> list[str]:
    law_ids = sorted({item.get("law_id") for item in records
                      if item.get("kind") == "observation"
                      and str(item.get("law_id", "")).startswith(f"{level}_")})
    out = []
    for law_id in law_ids:
        related = [item for item in records
                   if item.get("kind") == "observation"
                   and item.get("law_id") == law_id]
        if qualifies(
            [item["event_id"] for item in related],
            [item["similarity"] for item in related],
        ):
            out.append(law_id)
    return out


def laws_by_id(body: str, level: str) -> dict[str, dict]:
    return {f'{level}_{law["number"]}': law for law in _law_lines(body)}


def proposal_id(level: str, source_ids: list[str]) -> str:
    source = "|".join(sorted(source_ids)).encode()
    return f"prop_{level}_{hashlib.sha256(source).hexdigest()[:12]}"


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()
            if line.strip()]


def append_jsonl(path: Path, record: dict) -> None:
    records = read_jsonl(path)
    if any(item.get("id") == record.get("id") and
           item.get("kind") == record.get("kind") for item in records):
        return
    records.append(record)
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
    write_text_atomic(path, text)
