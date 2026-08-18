import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from decipher_core import (  # noqa: E402
    event_key,
    append_jsonl,
    qualified_law_ids,
    is_authorized,
    merge_level,
    merge_one,
    parse_proposal,
    pending_proposals,
    parse_analysis,
    qualifies,
    read_jsonl,
    text_similarity,
    write_text_atomic,
)


class IdentityTests(unittest.TestCase):
    def test_only_configured_owner_is_authorized(self):
        self.assertTrue(is_authorized(42, {42}))
        self.assertFalse(is_authorized(7, {42}))
        self.assertFalse(is_authorized(42, set()))

    def test_event_key_is_stable_from_telegram_update(self):
        self.assertEqual(event_key(123456), "evt_tg_123456")


class AnalysisTests(unittest.TestCase):
    def test_parses_grounded_candidate(self):
        event = "发布通知后无人检查回执，导致两人没有收到。"
        raw = json.dumps({
            "aspects": ["处世"],
            "judgment": "反馈没有收敛。",
            "laws": [{
                "statement": "发布责任止于反馈收敛。",
                "aspects": ["处世"],
                "evidence": "无人检查回执",
            }],
        }, ensure_ascii=False)

        out = parse_analysis(raw, event, "evt_tg_1")

        self.assertEqual(out["event_id"], "evt_tg_1")
        self.assertEqual(out["laws"][0]["evidence_ids"], ["evt_tg_1"])

    def test_rejects_non_json_output(self):
        with self.assertRaises(ValueError):
            parse_analysis("【一】自由文本", "事件", "evt_tg_1")

    def test_rejects_evidence_not_present_in_event(self):
        raw = json.dumps({
            "aspects": ["处世"],
            "judgment": "反馈没有收敛。",
            "laws": [{
                "statement": "发布责任止于反馈收敛。",
                "aspects": ["处世"],
                "evidence": "主人没有描述过的事实",
            }],
        }, ensure_ascii=False)
        with self.assertRaises(ValueError):
            parse_analysis(raw, "发布通知后无人检查回执。", "evt_tg_1")


class InductionTests(unittest.TestCase):
    def test_similarity_is_exact_for_same_law(self):
        self.assertEqual(text_similarity("反馈必须收敛", "反馈必须收敛"), 1.0)

    def test_similarity_does_not_confuse_unrelated_laws(self):
        self.assertLess(text_similarity("反馈必须收敛", "职责缺位须预设代位"), 0.9)

    def test_three_unique_evidences_at_threshold_qualify(self):
        self.assertTrue(qualifies(
            ["evt_1", "evt_2", "evt_3"],
            [1.0, 0.91, 0.90],
        ))

    def test_duplicate_or_subthreshold_evidence_does_not_qualify(self):
        self.assertFalse(qualifies(
            ["evt_1", "evt_1", "evt_2"],
            [1.0, 0.95, 0.94],
        ))
        self.assertFalse(qualifies(
            ["evt_1", "evt_2", "evt_3"],
            [1.0, 0.89, 0.99],
        ))

    def test_independent_law_is_appended_in_clean_form(self):
        body = "# 一 · 律\n\n1. 反馈必须收敛。（相：处世；据：evt_1）\n"
        candidate = {"statement": "职责缺位须预设代位。", "aspects": ["处世"]}

        out, record = merge_one(body, candidate, "evt_2")

        self.assertIn("2. 职责缺位须预设代位。（相：处世；据：evt_2）", out)
        self.assertEqual(record["action"], "new")
        self.assertEqual(record["law_id"], "一_2")

    def test_similar_law_only_gains_new_evidence(self):
        body = "# 一 · 律\n\n1. 反馈必须收敛。（相：处世；据：evt_1）\n"
        candidate = {"statement": "反馈必须收敛。", "aspects": ["处世"]}

        out, record = merge_one(body, candidate, "evt_2")

        self.assertEqual(out.count("反馈必须收敛"), 1)
        self.assertIn("据：evt_1, evt_2", out)
        self.assertEqual(record["action"], "reinforce")
        self.assertEqual(record["similarity"], 1.0)

    def test_duplicate_event_does_not_change_document(self):
        body = "# 一 · 律\n\n1. 反馈必须收敛。（相：处世；据：evt_1）\n"
        candidate = {"statement": "反馈必须收敛。", "aspects": ["处世"]}

        out, record = merge_one(body, candidate, "evt_1")

        self.assertEqual(out, body)
        self.assertEqual(record["action"], "duplicate")

    def test_higher_level_keeps_source_law_keys(self):
        body = "# 二 · 统\n"
        candidate = {"statement": "失序生于反馈未闭环。", "aspects": ["处世"]}

        out, record = merge_level(
            body, candidate, ["一_1", "一_2", "一_3"], "二",
        )

        self.assertIn("据：一_1, 一_2, 一_3", out)
        self.assertEqual(record["law_id"], "二_1")

    def test_proposal_must_cite_exact_qualified_sources(self):
        raw = json.dumps({
            "statement": "失序生于反馈未闭环。",
            "aspects": ["处世"],
            "source_ids": ["一_1", "一_2", "一_3"],
        }, ensure_ascii=False)
        proposal = parse_proposal(raw, "二", ["一_1", "一_2", "一_3"])
        self.assertEqual(proposal["level"], "二")
        with self.assertRaises(ValueError):
            parse_proposal(raw, "二", ["一_1", "一_2", "一_4"])

    def test_decided_proposal_is_not_pending(self):
        records = [
            {"kind": "proposal", "id": "prop_1", "level": "二"},
            {"kind": "proposal", "id": "prop_2", "level": "二"},
            {"kind": "decision", "id": "decision:prop_1", "proposal_id": "prop_1",
             "status": "approved"},
        ]
        self.assertEqual([item["id"] for item in pending_proposals(records)], ["prop_2"])

    def test_only_laws_with_three_qualified_observations_are_sources(self):
        records = []
        for law_id, scores in (("一_1", [1.0, 0.95, 0.9]),
                               ("一_2", [1.0, 0.95]),
                               ("二_1", [1.0, 0.95, 0.92])):
            for number, score in enumerate(scores):
                records.append({
                    "kind": "observation", "law_id": law_id,
                    "event_id": f"{law_id}_source_{number}", "similarity": score,
                })
        self.assertEqual(qualified_law_ids(records, "一"), ["一_1"])


class StorageTests(unittest.TestCase):
    def test_atomic_text_write_replaces_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "一.md"
            path.write_text("旧", encoding="utf-8")
            write_text_atomic(path, "新")
            self.assertEqual(path.read_text("utf-8"), "新")
            self.assertFalse((Path(directory) / "一.md.tmp").exists())

    def test_jsonl_reader_ignores_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(read_jsonl(Path(directory) / "ledger.jsonl"), [])

    def test_jsonl_append_is_idempotent_by_record_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            record = {"id": "evt_1", "kind": "event", "text": "事实"}
            append_jsonl(path, record)
            append_jsonl(path, record)
            self.assertEqual(read_jsonl(path), [record])


if __name__ == "__main__":
    unittest.main()
