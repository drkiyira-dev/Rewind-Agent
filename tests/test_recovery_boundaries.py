"""Identity and file-path boundaries must hold through public recovery calls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rewindagent.api import EffectView, LocalEffectSimulator, ToolSpec, resolve_effect
from rewindagent.durable import query_run, resume_run, start_run


class ReceiptIdentityTest(unittest.TestCase):
    def test_receipt_for_another_operation_does_not_publish_or_submit(self) -> None:
        simulator = LocalEffectSimulator(idempotent_keys=True, status_query=True)
        first = resolve_effect(EffectView("first", {"amount": 4}), simulator)
        self.assertTrue(first.published)
        receipt = first.values["receipt"]
        before = simulator.visible_log()

        refused = resolve_effect(
            EffectView(
                "second", {"amount": 4}, receipt=receipt,
                attempt_started=True, idempotent_keys=True, status_query=True,
            ),
            simulator,
        )

        self.assertEqual(refused.status, "safe_refusal")
        self.assertEqual(refused.reason_code, "receipt_mismatch")
        self.assertFalse(refused.published)
        self.assertIsNone(refused.values)
        self.assertEqual(simulator.visible_log(), before)
        self.assertIsNone(simulator.receipt("second"))

    def test_receipt_without_identity_is_not_proof_of_completion(self) -> None:
        simulator = LocalEffectSimulator(idempotent_keys=False, status_query=False)
        first = resolve_effect(EffectView("first", 4), simulator)
        receipt = dict(first.values["receipt"])
        del receipt["logical_id"]
        before = simulator.visible_log()

        refused = resolve_effect(EffectView("first", 4, receipt=receipt), simulator)

        self.assertEqual(refused.reason_code, "receipt_mismatch")
        self.assertFalse(refused.published)
        self.assertEqual(simulator.visible_log(), before)

    def test_matching_explicit_and_saved_receipts_still_reuse(self) -> None:
        simulator = LocalEffectSimulator(idempotent_keys=False, status_query=False)
        first = resolve_effect(EffectView("first", 4), simulator)
        receipt = first.values["receipt"]
        before = simulator.visible_log()

        for supplied in (receipt, None):
            with self.subTest(explicit=supplied is not None):
                reused = resolve_effect(
                    EffectView("first", 4, attempt_started=True, receipt=supplied), simulator,
                )
                self.assertTrue(reused.published)
                self.assertEqual(reused.reason_code, "effect_reused")
                self.assertEqual(reused.values["receipt"], receipt)
                self.assertEqual(simulator.visible_log(), before)


class StorePathTest(unittest.TestCase):
    def test_uri_characters_name_the_exact_store_across_start_query_resume(self) -> None:
        workflow = {
            "schema_version": 1,
            "steps": [{"step_id": "increment", "tool": "inc", "reads": ["x"], "writes": ["y"]}],
        }
        for filename in (
            "store#v1.sqlite", "store?v1.sqlite", "store%23v1.sqlite",
            "store%3Fv1.sqlite", "store space.sqlite", "数据.sqlite",
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(prefix="rewind-path-") as tmp:
                root = Path(tmp)
                store = root / filename
                calls = []

                def increment(values):
                    calls.append(list(values))
                    return (values[0] + 1,)

                tools = [ToolSpec("inc", increment, impl_id="path-test.inc.v1")]
                started = start_run(store, "run", workflow, {"x": 1}, tools)
                self.assertTrue(started.published)
                self.assertTrue(store.is_file())
                self.assertEqual({path.name for path in root.iterdir()}, {filename, filename + ".lock"})

                before = store.read_bytes(), store.stat().st_mtime_ns
                queried = query_run(store, "run")
                self.assertTrue(queried.published)
                self.assertEqual(queried.values, {"x": 1, "y": 2})
                self.assertEqual((store.read_bytes(), store.stat().st_mtime_ns), before)

                resumed = resume_run(store, "run", tools)
                self.assertTrue(resumed.published)
                self.assertEqual(resumed.publication_id, started.publication_id)
                self.assertEqual(resumed.values, queried.values)
                self.assertEqual(calls, [[1]])
                self.assertEqual({path.name for path in root.iterdir()}, {filename, filename + ".lock"})
