"""Local original-evidence audit, explicitly excluded from portable discovery.

Run with: python3 -m unittest tests.history_audit -v
Missing original evidence is an error; there is no synthetic replacement or skip.
"""
import hashlib
import json
import unittest
from pathlib import Path
from rewindagent.evaluate import run_suite
from tests.test_round2 import SEMANTIC_KEYS

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {'results/eval-rerun.json': '6a43d6affb1160a85cf5d4e390f40a68470fdf68ccf47c98191525b98ac074ec', 'results/eval.json': '7f1c2e8462886005c24cb1cf2823386c9c86c180e72e468f5e7eec760a2d68e4', 'results/phase2a/pure-crash/crash-results.json': '7373b9ed7265085a9b58b54132e50c8916f9137c99024de9048d02c655bf793e', 'results/phase2a/pure-crash/installed-k3.json': 'c143b7e727c5bd2f205d3aee01b85070aede0fd50369a5bcb48391a864ed95bd', 'results/phase2b/effect-crash/crash-results.json': '9741b8de87c02858e4871d6db4e03fa79ff659bc7d7e373356523ad6dc81afff', 'results/phase2b/effect-crash/installed.json': 'f93c5583e9cae7d3b665e461011c226640584aed6c8a3750f68452068581f443', 'results/round2.json': 'b6b8d91a2b7b1f6fd56394ff8a9d311ba9c96fb965472d00cd76bc98f82f60fa'}


class HistoricalEvidenceAudit(unittest.TestCase):
    def test_historical_result_files_keep_their_hashes(self) -> None:
        for name, digest in FROZEN.items():
            data = (ROOT / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)


    def test_historical_files_unchanged(self) -> None:
        for name, digest in FROZEN.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)


    def test_original_eval_semantics_still_match(self) -> None:
        stored = json.loads((ROOT / "results" / "eval.json").read_text(encoding="utf-8"))
        fresh = run_suite()
        stored_rows = [{key: row[key] for key in SEMANTIC_KEYS} for row in stored["rows"]]
        fresh_rows = [{key: row[key] for key in SEMANTIC_KEYS} for row in fresh["rows"]]
        self.assertEqual(fresh_rows, stored_rows)
        for old, new in zip(stored["rows"], fresh["rows"]):
            if old["method"] == "B4":
                self.assertEqual(new["closure"], old["closure"])
        summary_keys = [
            "attempts",
            "b4_eligible_success_count",
            "b4_eligible_count",
            "recovery_success_count",
            "pure_b4_recompute_equals_b3_on_every_eligible_case",
            "benefit_vs_suffix_on_diamond",
            "linear_b4_recompute_equals_b2",
            "b4_cases_with_safety_failures",
            "duplicate_applied_rows",
        ]
        for key in summary_keys:
            self.assertEqual(fresh["summary"][key], stored["summary"][key])
