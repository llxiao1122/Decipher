import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import telegram_receiver as receiver  # noqa: E402


class AuthorizationTests(unittest.TestCase):
    def test_owner_ids_are_parsed_from_csv(self):
        self.assertEqual(receiver._owner_ids("42, 81"), {42, 81})

    @patch.object(receiver, "_reply_stream")
    @patch.object(receiver, "send_message")
    def test_unauthorized_message_never_reaches_analysis(self, send, reply):
        accepted = receiver._handle(100, 7, "敏感事件", owners={42})

        self.assertFalse(accepted)
        reply.assert_not_called()
        send.assert_called_once_with(7, "无权访问。")

    @patch.object(receiver, "_analyze_event", side_effect=OSError("network"))
    @patch.object(receiver, "send_markdown")
    @patch.object(receiver, "send_message", return_value={})
    @patch.object(receiver, "logger")
    def test_transient_analysis_failure_requests_retry(
            self, _logger, _send, _markdown, _analyze):
        self.assertIsNone(receiver._reply_stream(42, "事件", "evt_tg_1"))

    @patch.object(receiver, "_reply_stream", return_value=None)
    @patch.object(receiver, "logger")
    def test_handler_propagates_retry_signal(self, _logger, _reply):
        self.assertIsNone(receiver._handle(1, 42, "事件", owners={42}))


class IngestionTests(unittest.TestCase):
    def test_valid_analysis_updates_clean_document_and_ledger(self):
        raw = json.dumps({
            "aspects": ["处世"],
            "judgment": "口径有始，反馈无终。",
            "laws": [{
                "statement": "发布责任止于反馈收敛。",
                "aspects": ["处世"],
                "evidence": "无人检查回执",
            }],
        }, ensure_ascii=False)
        event = "发布通知后无人检查回执，导致两人没有收到。"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "一.md"
            ledger = root / "ledger.jsonl"
            one.write_text("# 一 · 律\n", encoding="utf-8")

            reply = receiver._apply_analysis(raw, event, "evt_tg_9", one, ledger)

            self.assertIn("【道判】", reply)
            self.assertIn("未升维（累计不足）", reply)
            self.assertIn(
                "1. 发布责任止于反馈收敛。（相：处世；据：evt_tg_9）",
                one.read_text("utf-8"),
            )
            records = receiver.read_jsonl(ledger)
            self.assertEqual(records[0]["kind"], "event")
            self.assertEqual(records[1]["kind"], "observation")

    def test_third_matching_event_reaches_observation_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "一.md"
            ledger = root / "ledger.jsonl"
            one.write_text("# 一 · 律\n", encoding="utf-8")
            for number in range(1, 4):
                event = f"第{number}次发生无人检查回执。"
                raw = json.dumps({
                    "aspects": ["处世"],
                    "judgment": "反馈无终。",
                    "laws": [{
                        "statement": "发布责任止于反馈收敛。",
                        "aspects": ["处世"],
                        "evidence": "无人检查回执",
                    }],
                }, ensure_ascii=False)
                reply = receiver._apply_analysis(
                    raw, event, f"evt_tg_{number}", one, ledger,
                )

            self.assertIn("已达升维观察门槛", reply)


class DecisionTests(unittest.TestCase):
    def test_approval_writes_higher_law_then_records_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            two = root / "二.md"
            three = root / "三.md"
            two.write_text("# 二 · 统\n", encoding="utf-8")
            three.write_text("# 三 · 势\n", encoding="utf-8")
            receiver.append_jsonl(ledger, {
                "kind": "proposal", "id": "prop_1", "level": "二",
                "statement": "失序生于反馈未闭环。", "aspects": ["处世"],
                "source_ids": ["一_1", "一_2", "一_3"],
            })

            result = receiver._decide(
                "prop_1", "approved", ledger, {"二": two, "三": three},
            )

            self.assertIn("已批准", result)
            self.assertIn("据：一_1, 一_2, 一_3", two.read_text("utf-8"))
            self.assertEqual(receiver.pending_proposals(receiver.read_jsonl(ledger)), [])

    def test_rejection_does_not_change_cognition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            two = root / "二.md"
            three = root / "三.md"
            two.write_text("# 二 · 统\n", encoding="utf-8")
            three.write_text("# 三 · 势\n", encoding="utf-8")
            receiver.append_jsonl(ledger, {
                "kind": "proposal", "id": "prop_1", "level": "二",
                "statement": "失序生于反馈未闭环。", "aspects": ["处世"],
                "source_ids": ["一_1", "一_2", "一_3"],
            })

            receiver._decide(
                "prop_1", "rejected", ledger, {"二": two, "三": three},
            )

            self.assertEqual(two.read_text("utf-8"), "# 二 · 统\n")

    @patch.object(receiver, "_llm")
    def test_three_qualified_laws_create_one_pending_proposal(self, llm):
        llm.return_value = json.dumps({
            "statement": "失序生于关系未收敛。",
            "aspects": ["处世"],
            "source_ids": ["一_1", "一_2", "一_3"],
        }, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            one = root / "一.md"
            one.write_text(
                "# 一 · 律\n"
                "1. 反馈必须收敛。（相：处世；据：evt_1）\n"
                "2. 授权必须闭环。（相：处世；据：evt_2）\n"
                "3. 口径必须统一。（相：处世；据：evt_3）\n",
                encoding="utf-8",
            )
            for law_id in ("一_1", "一_2", "一_3"):
                for number in range(3):
                    receiver.append_jsonl(ledger, {
                        "kind": "observation", "id": f"{law_id}:{number}",
                        "law_id": law_id, "event_id": f"evt_{law_id}_{number}",
                        "similarity": 1.0,
                    })

            proposal = receiver._maybe_propose("二", "一", one, ledger)

            self.assertEqual(proposal["level"], "二")
            self.assertEqual(len(receiver.pending_proposals(receiver.read_jsonl(ledger))), 1)


if __name__ == "__main__":
    unittest.main()
