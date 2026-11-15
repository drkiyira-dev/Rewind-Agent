"""Test-only SIGKILL rendezvous around agent transactions; never a runtime hook.

Run as a module with WINDOW READY followed by responses_agent CLI arguments.
The parent owns the process and kills it after the atomic ready-file rename.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from rewindagent import agent
from examples.responses_agent import main


def run():
    window, filename, *args = sys.argv[1:]
    target = window.removeprefix('hot_')
    ready = Path(filename)
    original = agent.connect

    def pause():
        pending = ready.with_suffix('.pending')
        pending.write_text(json.dumps({'pid': os.getpid(), 'point': window}))
        pending.replace(ready)
        time.sleep(30)
        raise RuntimeError('parent did not kill transaction worker')

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *parameters):
            if sql == 'COMMIT':
                row = self.conn.execute('SELECT * FROM agent_sessions LIMIT 1').fetchone()
                if row:
                    phase = row['phase']
                    before = (
                        (target == 'intent_uncommitted' and phase == 'may_have_sent')
                        or (target == 'call_uncommitted' and phase == 'tool_pending')
                        or (target == 'effect_output_uncommitted' and phase == 'awaiting_model'
                            and self.conn.execute("SELECT 1 FROM agent_calls WHERE name='record_summary' AND adopted=1").fetchone())
                        or (target == 'final_uncommitted' and phase == 'final_saved')
                        or (target == 'publication_uncommitted' and phase == 'published')
                    )
                    if before:
                        if window.startswith('hot_'):
                            # Test-only uncommitted padding forces cache spill;
                            # recovery must roll back this row as well.
                            self.conn.execute(
                                "INSERT INTO agent_items VALUES (?, 999999, 'rollback_padding', ?)",
                                (row['run_id'], json.dumps({'padding': 'x' * 262144})),
                            )
                        pause()
                    if target == 'intent_saved' and phase == 'may_have_sent':
                        result = self.conn.execute(sql, *parameters)
                        pause()
                        return result
            return self.conn.execute(sql, *parameters)

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        if window.startswith('hot_') and kwargs.get('write'):
            # Force dirty pages out before COMMIT to leave a genuine hot
            # rollback journal, not only uncommitted pages in the page cache.
            conn.execute('PRAGMA cache_size=1')
            conn.execute('PRAGMA cache_spill=ON')
        return Connection(conn)

    with patch.object(agent, 'connect', connect):
        return main(args)


if __name__ == '__main__':
    raise SystemExit(run())
