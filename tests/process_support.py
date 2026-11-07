"""Own and reap only subprocesses created by the current test."""

import subprocess
import os
import tempfile
from pathlib import Path

_evidence_dir = None


def evidence_path(name: str) -> Path:
    """Never overwrite an earlier test run, even with an explicit output parent."""
    global _evidence_dir
    if _evidence_dir is None:
        parent = os.environ.get("REWIND_TEST_OUTPUT")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        _evidence_dir = Path(tempfile.mkdtemp(prefix="rewind-tests-", dir=parent))
    return _evidence_dir / name


def stop_process(proc: subprocess.Popen) -> int:
    """Keep SIGKILL fault injection, then drain pipes and reap the child."""
    if proc.poll() is None:
        proc.kill()
    streams = (proc.stdin, proc.stdout, proc.stderr)
    if any(stream is not None and not stream.closed for stream in streams):
        proc.communicate(timeout=10)
    else:
        proc.wait(timeout=10)
    for stream in streams:
        if stream is not None:
            stream.close()
    return proc.returncode


def owned_popen(owner, *args, **kwargs) -> subprocess.Popen:
    proc = subprocess.Popen(*args, **kwargs)
    # Register immediately: startup failure, timeout and mid-test assertions
    # must not depend on the caller reaching its normal finally block.
    owner.addCleanup(stop_process, proc)
    return proc
