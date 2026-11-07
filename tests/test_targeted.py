"""Hand-written cases. Expected slot values live in the scenarios, not in the verifier."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from rewindagent.canonical import semantic_hash
from rewindagent.cases import hand_scenarios
from rewindagent.engine import Engine, Fault, record, rerun
from rewindagent.evaluate import run_case
from rewindagent.model import Conflict, Ledger, Log, LogCorrupt, State, default_tools, load_snapshot, snapshot
from rewindagent.verify import contaminates, verify_reuse


ROOT = Path(__file__).resolve().parents[1]


def _case(case_id: str):
    return next(item for item in hand_scenarios() if item.case_id == case_id)


def _rows(case_id: str) -> dict:
    return {row["method"]: row for row in run_case(_case(case_id))}


def _save(name: str, payload: dict) -> Path:
    path = ROOT / "tests" / "regressions" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class TargetedRecoveryTest(unittest.TestCase):
    def test_pre_registered_b4_status_and_success_flag(self) -> None:
        for case in hand_scenarios():
            with self.subTest(case=case.case_id):
                row = _rows(case.case_id)["B4"]
                self.assertEqual(row["status"], case.expected_b4)
                self.assertEqual(row["recovery_success"], case.success_eligible)
                if row["status"] != "recovered":
                    self.assertFalse(row["recovery_success"])
                self.assertEqual(row["actual_model_calls"], 0)
                self.assertEqual(row["actual_token_usage"], 0)
                self.assertEqual(row["failure_localization"], "given")
                self.assertEqual(row["duplicate_applied"], 0)

    def test_linear_has_no_advantage_over_suffix(self) -> None:
        rows = _rows("linear")
        self.assertEqual(rows["B4"]["recomputed"], ["B", "C"])
        self.assertEqual(rows["B4"]["reused"], ["A"])
        self.assertEqual(rows["B2"]["recomputed"], ["B", "C"])
        self.assertEqual(rows["B2"]["prefix_skipped"], ["A"])
        self.assertEqual(rows["B2"]["reused"], [])
        self.assertTrue(rows["B4"]["b1_equals_b2"])
        self.assertEqual(rows["B4"]["recomputed"], rows["B3"]["recomputed"])
        self.assertEqual(rows["B1"]["recomputed"], rows["B2"]["recomputed"])
        self.assertTrue(rows["B1"]["used_checkpoint"])
        self.assertFalse(rows["B4"]["used_checkpoint"])

    def test_diamond_reuses_independent_branch(self) -> None:
        rows = _rows("diamond")
        self.assertEqual(rows["B4"]["recomputed"], ["L", "J"])
        self.assertEqual(rows["B4"]["reused"], ["P", "S", "R"])
        self.assertEqual(rows["B2"]["recomputed"], ["L", "R", "J"])
        self.assertEqual(rows["B2"]["prefix_skipped"], ["P", "S"])
        self.assertLess(rows["B4"]["recomputed_count"], rows["B2"]["recomputed_count"])
        self.assertEqual(rows["B1"]["recomputed"], ["S", "L", "R", "J"])
        self.assertFalse(rows["B4"]["b1_equals_b2"])
        self.assertEqual(rows["B4"]["recomputed"], rows["B3"]["recomputed"])
        self.assertFalse(rows["B4"]["used_checkpoint"])
        self.assertTrue(rows["B1"]["used_checkpoint"])
        self.assertEqual(set(rows["B4"]["closure"]), {"L", "J"})
        self.assertFalse(set(rows["B4"]["reused"]) & set(rows["B4"]["closure"]))

    def test_multi_reader_invalidates_every_reader(self) -> None:
        case = _case("multi_reader")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        self.assertEqual(contaminates(recording.events, "W"), {"W", "A", "B"})
        readers = [event["instance_id"] for event in recording.events if any(read["producer"] == "W" for read in event["reads"])]
        self.assertEqual(readers, ["A", "B"])
        row = _rows("multi_reader")["B4"]
        self.assertEqual(row["recomputed"], ["W", "A", "B"])
        self.assertEqual(row["reused"], [])
        if row["oracle_match"] is not True:
            path = _save("multi_reader", row)
            self.fail(path)

    def test_multi_version_binds_the_read_not_the_slot_name(self) -> None:
        case = _case("multi_version")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        by_id = {event["instance_id"]: event for event in recording.events}
        self.assertEqual(by_id["R1"]["reads"][0]["producer"], "W1")
        self.assertEqual(by_id["R2"]["reads"][0]["producer"], "W2")
        self.assertEqual(by_id["W2"]["reads"], [])
        self.assertEqual(by_id["W2"]["order_ww"][0]["kind"], "write_write_or_blind")
        self.assertEqual(contaminates(recording.events, "W1"), {"W1", "R1"})
        row = _rows("multi_version")["B4"]
        self.assertEqual(row["recomputed"], ["W1", "R1"])
        self.assertEqual(row["reused"], ["W2", "R2"])
        self.assertEqual(set(row["closure"]), {"W1", "R1"})

    def test_control_abandons_old_branch(self) -> None:
        row = _rows("control_flip")["B4"]
        self.assertIn("A", row["abandoned"])
        self.assertIn("B", row["new_events"])
        self.assertNotIn("A", row["recomputed"])
        self.assertNotIn("A", row["reused"])
        self.assertEqual(row["oracle_match"], True)

    def test_loop_unfolded_without_a_cycle(self) -> None:
        case = _case("loop_carried")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        order = [event["instance_id"] for event in recording.events]
        self.assertEqual(order, ["init", "body#0", "body#1", "body#2"])
        position = {item: index for index, item in enumerate(order)}
        for event in recording.events:
            for read in event["reads"]:
                producer = read["producer"]
                if producer in position:
                    self.assertLess(position[producer], position[event["instance_id"]])
        row = _rows("loop_carried")["B4"]
        self.assertEqual(row["recomputed"], ["body#0", "body#1", "body#2"])
        self.assertEqual(row["reused"], ["init"])
        self.assertEqual(set(row["closure"]), {"body#0", "body#1", "body#2"})
        self.assertFalse(set(row["reused"]) & set(row["closure"]))

    def test_new_event_is_executed(self) -> None:
        row = _rows("new_event")["B4"]
        self.assertIn("extra", row["new_events"])
        self.assertIn("extra", row["recomputed"])
        self.assertEqual(row["reused"], ["base"])
        self.assertEqual(row["oracle_match"], True)

    def test_undeclared_read_blocks_only_the_contract_cache(self) -> None:
        rows = _rows("undeclared")
        self.assertEqual(rows["B4"]["recomputed"], ["spy"])
        self.assertNotIn("spy", rows["B4"]["reused"])
        self.assertTrue(rows["B4"]["recovery_success"])
        self.assertEqual(rows["B4"]["safety_failures"], [])
        self.assertEqual(rows["B3"]["reused"], ["spy"])
        self.assertIn("unsafe_reuse_undeclared", rows["B3"]["safety_failures"])
        self.assertFalse(rows["B3"]["recovery_success"])
        self.assertFalse(rows["B3"]["oracle_match"])

    def test_escape_refuses_selective_recovery_only(self) -> None:
        rows = _rows("escape")
        self.assertEqual(rows["B4"]["status"], "refused_unobservable_dependency")
        self.assertFalse(rows["B4"]["recovery_success"])
        self.assertNotEqual(rows["B0"]["status"], "refused_unobservable_dependency")
        self.assertNotEqual(rows["B3"]["status"], "refused_unobservable_dependency")

    def test_impl_change_misses_cache(self) -> None:
        rows = _rows("impl_change")
        self.assertEqual(rows["B4"]["recomputed"], ["B"])
        self.assertEqual(rows["B4"]["reused"], ["A"])
        self.assertTrue(rows["B4"]["oracle_match"])
        self.assertEqual(rows["B1"]["status"], "no_checkpoint")
        self.assertEqual(rows["B2"]["status"], "not_applicable")
        self.assertEqual(rows["B4"]["recomputed"], rows["B3"]["recomputed"])

    def test_config_change_misses_cache(self) -> None:
        def original(engine: Engine) -> None:
            engine.call("A", "set", [], ["a"], config={"value": 3})
            engine.call("B", "inc", ["a"], ["b"])

        def changed(engine: Engine) -> None:
            engine.call("A", "set", [], ["a"], config={"value": 4})
            engine.call("B", "inc", ["a"], ["b"])

        recording = record(original, {}, None, default_tools())
        result = rerun("B4", changed, recording, {}, tools=default_tools())
        self.assertEqual(result.values, {"a": 4, "b": 5})
        self.assertEqual(result.recomputed, ["A", "B"])

    def test_unbounded_refused(self) -> None:
        case = _case("unbounded")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        self.assertEqual(recording.status, "refused_unbounded_control")
        row = _rows("unbounded")["B4"]
        self.assertEqual(row["status"], "refused_unbounded_control")
        self.assertFalse(row["recovery_success"])

    def test_same_external_payload_is_not_sent_again(self) -> None:
        case = _case("irreversible_same")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        rows = _rows("irreversible_same")
        self.assertEqual(len(recording.ledger_entries), 1)
        self.assertEqual(rows["B4"]["external_attempts"], 0)
        self.assertEqual(rows["B4"]["reused"], ["msg", "mail"])
        self.assertTrue(rows["B4"]["ledger_unchanged"])
        self.assertTrue(rows["B4"]["recovery_success"])
        self.assertEqual(rows["B0"]["status"], "effect_replay_suppressed")
        self.assertGreater(rows["B0"]["external_attempts"], 0)
        self.assertEqual(rows["B0"]["duplicate_applied"], 0)
        self.assertFalse(rows["B0"]["recovery_success"])
        self.assertTrue(rows["B0"]["ledger_unchanged"])
        self.assertEqual(rows["B0"]["ledger_entries"], recording.ledger_entries)

    def test_changed_external_payload_keeps_the_old_ledger(self) -> None:
        case = _case("irreversible_changed")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        before = [dict(entry) for entry in recording.ledger_entries]
        result = rerun("B4", case.workflow, recording, case.initial, tools=default_tools())
        self.assertEqual(result.status, "refused_external_payload_changed")
        self.assertEqual(result.ledger_entries, before)
        self.assertEqual(result.external_attempts, 0)
        self.assertEqual(result.duplicate_applied, 0)
        self.assertEqual(recording.ledger_entries, before)
        self.assertEqual(result.ledger_entries[0]["payload_hash"], semantic_hash([1002]))
        self.assertFalse(result.published)
        self.assertIsNone(result.published_values)
        self.assertEqual(result.candidate_values.get("m"), 2)
        self.assertIsNone(result.values)
        self.assertNotEqual(result.status, "recovered")
        rows = _rows("irreversible_changed")
        self.assertEqual(rows["B0"]["status"], "external_blocked")
        self.assertEqual(rows["B0"]["duplicate_applied"], 0)
        self.assertTrue(rows["B0"]["ledger_unchanged"])
        self.assertFalse(rows["B4"]["recovery_success"])

    def test_persistent_fault_stops_once(self) -> None:
        row = _rows("persistent")["B4"]
        self.assertEqual(row["status"], "persistent_fault")
        self.assertEqual(row["recomputed"].count("B"), 1)
        self.assertNotIn("C", row["recomputed"])
        self.assertNotIn("B", row["abandoned"])
        self.assertIn("C", row["abandoned"])
        self.assertFalse(row["recovery_success"])
        self.assertEqual(_rows("persistent")["B0"]["status"], "persistent_fault")

    def test_write_guard_does_not_assign(self) -> None:
        state = State()
        state.init("x", 5)
        before = (state.slots["x"].value, state.slots["x"].version, state.slots["x"].value_hash)
        with self.assertRaises(Conflict):
            state.write("x", 9, "later", expected_base_hash="not-the-current-hash")
        self.assertEqual(
            (state.slots["x"].value, state.slots["x"].version, state.slots["x"].value_hash),
            before,
        )
        engine = Engine(mode="recovery")
        engine.state.init("x", 5)
        observed = [
            {
                "slot": "x",
                "value_hash": "stale",
                "producer": "old",
                "version": 0,
                "reader": "n",
                "value": 1,
            }
        ]
        with self.assertRaises(Conflict):
            engine._apply_outputs("n", ["x"], ["x"], (9,), observed)
        self.assertEqual(engine.state.slots["x"].value, 5)

    def test_corrupt_log_and_checkpoint_fail_closed(self) -> None:
        case = _case("diamond")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        loaded = Log.load(recording.log_text)
        self.assertEqual(loaded.records[0]["schema_version"], 1)
        lines = [json.loads(line) for line in recording.log_text.splitlines() if line.strip()]
        lines[1]["hash"] = "f" * 64
        bad = "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)
        with self.assertRaises(LogCorrupt):
            Log.load(bad)
        lines[1]["hash"] = loaded.records[1]["hash"]
        lines[1]["prev"] = "0" * 64
        bad_prev = "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)
        with self.assertRaises(LogCorrupt):
            Log.load(bad_prev)
        recording.ledger_entries.append({"id": "mail", "payload_hash": "abc"})
        recording.marked[0]["content_hash"] = "0" * 64
        stopped = rerun("B1", case.workflow, recording, case.initial, tools=default_tools())
        self.assertEqual(stopped.status, "checkpoint_corrupt")
        self.assertNotEqual(stopped.status, "recovered")
        self.assertEqual(stopped.ledger_entries, [{"id": "mail", "payload_hash": "abc"}])
        with self.assertRaises(LogCorrupt):
            load_snapshot(recording.marked[0])

    def test_snapshot_does_not_contain_or_clear_the_ledger(self) -> None:
        state = State()
        state.init("a", 1)
        ledger = Ledger()
        self.assertEqual(ledger.deliver("mail", "abc"), "appended")
        blob = snapshot(state, "a")
        self.assertNotIn("ledger", blob["body"])
        restored = load_snapshot(blob)
        self.assertEqual(restored.values(), {"a": 1})
        self.assertEqual(ledger.entries, [{"id": "mail", "payload_hash": "abc"}])
        self.assertEqual(ledger.deliver("mail", "abc"), "suppressed")
        self.assertEqual(len(ledger.entries), 1)
        self.assertEqual(ledger.deliver("mail", "other"), "blocked")
        self.assertEqual(ledger.entries[0]["payload_hash"], "abc")

    def test_bad_plan_is_rejected_without_an_oracle(self) -> None:
        events = [
            {
                "instance_id": "E1",
                "reads": [],
                "control_reads": [],
                "output_hash": "bad",
                "violation": False,
                "escapes": False,
            },
            {
                "instance_id": "E2",
                "reads": [{"producer": "E1", "slot": "x", "value_hash": "bad"}],
                "control_reads": [],
                "output_hash": "a",
                "violation": False,
                "escapes": False,
            },
            {
                "instance_id": "E3",
                "reads": [{"producer": "E1", "slot": "x", "value_hash": "bad"}],
                "control_reads": [],
                "output_hash": "b",
                "violation": False,
                "escapes": False,
            },
        ]
        self.assertEqual(contaminates(events, "E1"), {"E1", "E2", "E3"})
        for reader in ("E2", "E3"):
            problems = verify_reuse(events, "E1", {reader}, {})
            self.assertIn(f"reuse_of_unrecomputed_fault_reader:{reader}", problems)
        self.assertEqual(verify_reuse(events, "E1", set(), {}), [])

    def test_semantic_hash_keeps_bools_and_omits_run_identity(self) -> None:
        self.assertNotEqual(semantic_hash(True), semantic_hash(1))
        self.assertNotEqual(semantic_hash(False), semantic_hash(0))
        key = {"inst": "A", "impl": "inc.v1", "config": {"value": 1}, "inputs": ["abc"]}
        self.assertEqual(semantic_hash(key), semantic_hash(dict(key)))
        self.assertNotEqual(semantic_hash(key), semantic_hash({**key, "run_id": "run-1"}))
        source = (ROOT / "rewindagent" / "engine.py").read_text(encoding="utf-8")
        self.assertNotIn("run_id", source)
        verifier = (ROOT / "rewindagent" / "verify.py").read_text(encoding="utf-8")
        self.assertNotIn("expected_values", verifier)
        self.assertNotIn("evaluate_graph", verifier)
        self.assertNotIn("oracle", verifier)
        reference = (ROOT / "rewindagent" / "reference.py").read_text(encoding="utf-8")
        self.assertNotIn("from .engine", reference)
        self.assertNotIn("import engine", reference)
        self.assertNotIn("contaminates", reference)

    def test_genesis_producer_and_repeatable_result(self) -> None:
        state = State()
        state.init("n", 1)
        self.assertEqual(state.slots["n"].producer, "genesis")
        self.assertEqual(state.slots["n"].version, 0)
        case = _case("diamond")
        tools = default_tools()
        recording = record(case.workflow, case.initial, case.fault, tools)
        first = rerun("B4", case.workflow, recording, case.initial, tools=tools)
        second = rerun("B4", case.workflow, recording, case.initial, tools=tools)
        self.assertEqual(first.values, second.values)
        self.assertEqual(first.recomputed, second.recomputed)
        self.assertEqual(first.reused, second.reused)
        text = recording.log_text
        self.assertEqual(recording.log_text, text)
        self.assertEqual(first.values["l"], 3)
        self.assertNotEqual(recording.values["l"], 3)


if __name__ == "__main__":
    unittest.main()
