"""Retry deadlines use the send-time clock, including persisted fault boundaries."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rewindagent.durable import Boundary, resume_run, start_run
from rewindagent.effectsvc import CONTRACT_VERSION, SERVICE_ID

WORKFLOW = {"schema_version": 1, "steps": [{
    "step_id": "append", "kind": "effect", "logical_id": "record",
    "service_id": SERVICE_ID, "reads": ["value"], "writes": ["receipt"],
}]}


class _Service:
    service_id = SERVICE_ID

    def __init__(self, clock):
        self.clock = clock
        self.posts = []
        self.capabilities_delay = 0

    def capabilities(self):
        self.clock[0] += self.capabilities_delay
        return {"service_id": SERVICE_ID, "contract_version": CONTRACT_VERSION,
                "idempotent": True, "query": False, "retention_ms": 1000}

    def submit(self, request):
        self.posts.append((self.clock[0], dict(request)))
        if len(self.posts) == 1:
            return {"status": "unavailable"}
        return {"status": "confirmed", "effect_id": "same-effect",
                **{key: request[key] for key in
                   ("idempotency_key", "logical_id", "payload_digest")}}


class EffectRetentionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Path(folder.name) / "task.sqlite"
        self.clock = [1000]
        self.service = _Service(self.clock)
        self.services = {SERVICE_ID: self.service}
        clock_patch = patch("time.time", side_effect=lambda: self.clock[0] / 1000)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        result = start_run(self.store, "retry", WORKFLOW, {"value": 7}, [], services=self.services)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(self.service.posts), 1)

    def resume(self, **kwargs):
        return resume_run(self.store, "retry", [], services=self.services, **kwargs)

    def assert_expired(self, result):
        self.assertEqual(result.reason_code, "retention_expired")
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.published)
        self.assertIsNone(result.values)
        self.assertEqual(len(self.service.posts), 1)

    def test_slow_capabilities_cannot_extend_retry_window(self):
        self.clock[0] = 1500
        self.service.capabilities_delay = 1000
        self.assert_expired(self.resume())

    def test_pause_after_attempt_commit_rechecks_before_post(self):
        self.clock[0] = 1500

        def pause(_boundary, point):
            if point == "E2":
                self.clock[0] = 2500

        with patch.object(Boundary, "reach", pause):
            self.assert_expired(self.resume())
        # The durable attempt remains conservative even though no POST followed.
        conn = sqlite3.connect(self.store)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM effect_attempts").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT state FROM effect_ops").fetchone()[0], "unknown")
        finally:
            conn.close()

    def test_exact_deadline_and_clock_before_preparation_do_not_retry(self):
        for instant in (2000, 999):
            with self.subTest(now_ms=instant):
                self.clock[0] = instant
                self.assert_expired(self.resume())

    def test_unexpired_retry_preserves_identity(self):
        self.clock[0] = 1999
        result = self.resume()
        self.assertTrue(result.published)
        self.assertEqual(len(self.service.posts), 2)
        self.assertEqual(self.service.posts[0][1], self.service.posts[1][1])

    def test_explicit_test_clock_remains_deterministic(self):
        self.clock[0] = 5000
        result = self.resume(now_ms=1500)
        self.assertTrue(result.published)
        self.assertEqual(len(self.service.posts), 2)
