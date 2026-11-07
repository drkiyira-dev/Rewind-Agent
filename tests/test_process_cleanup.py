"""Exercise abnormal exits with real children, not process-name heuristics."""

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.process_support import owned_popen, stop_process
from tests import test_durable_crash as durable
from tests import test_finalization as finalization


class RecordingOwner(unittest.TestCase):
    def __init__(self):
        super().__init__()
        self.children = []

    def addCleanup(self, function, *args, **kwargs):
        if function is stop_process:
            self.children.append(args[0])
        super().addCleanup(function, *args, **kwargs)


class ProcessCleanupTest(unittest.TestCase):
    def assert_reaped(self, proc):
        self.assertIsNotNone(proc.returncode)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            self.assertTrue(stream is None or stream.closed)
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)

    def test_failed_assertion_reaps_child_without_pipes(self):
        owner = RecordingOwner()
        def fail_midway():
            owned_popen(owner, [sys.executable, "-c", "import time; time.sleep(60)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            owner.fail("injected assertion failure")
        owner.runTest = fail_midway
        result = unittest.TestResult()
        owner.run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(owner.children[0].returncode, -signal.SIGKILL)
        self.assert_reaped(owner.children[0])

    def test_timeout_drains_both_pipes(self):
        proc = owned_popen(self, [sys.executable, "-c",
            "import os,time; os.write(1,b'x'*100000); os.write(2,b'y'*100000); time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.communicate(timeout=0.1)
        stop_process(proc)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assert_reaped(proc)

    def test_failure_at_crash_boundary_reaps_worker(self):
        owner = RecordingOwner()
        self.addCleanup(owner.doCleanups)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def fail():
                raise AssertionError("injected boundary assertion")
            with self.assertRaisesRegex(AssertionError, "injected boundary"):
                durable._crash(owner, durable._start_args(root / "t.sqlite", "run", root / "obs.jsonl"),
                               root / "ready.json", "K3", before_kill=fail)
            self.assertEqual(owner.children[0].returncode, -signal.SIGKILL)
            self.assert_reaped(owner.children[0])

    def test_startup_failure_reaps_service(self):
        owner = RecordingOwner()
        self.addCleanup(owner.doCleanups)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AssertionError):
                finalization._serve(owner, Path(tmp), "invalid-profile")
        self.assertEqual(owner.children[0].returncode, 2)
        self.assert_reaped(owner.children[0])
