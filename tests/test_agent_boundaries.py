"""3A boundaries tested without a provider connection or a model credential."""

import dataclasses
import gc
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from rewindagent import agent
from rewindagent.agent import start_agent, resume_agent, query_agent, inspect_agent
from rewindagent.agent_store import SessionLock, connect
from rewindagent.adapters.openai_responses import ResponsesConfig, config_problem, create_response
from rewindagent.api import ToolSpec
from rewindagent.durable import start_run
from examples.responses_agent import tools

PROVIDER = ResponsesConfig('local_protocol_fixture', 'fixture-responses-1', 'http://127.0.0.1:1/v1')
SNAPSHOT = {'records': [{'n': 3}, {'n': 4}]}


def call(name='read_snapshot', call_id='first', status='completed'):
    return {'type': 'function_call', 'call_id': call_id, 'name': name, 'arguments': '{}', 'status': status}


def response(*items):
    return {'ok': True, 'response': {'status': 'completed', 'output': list(items)}}


class AgentBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='rewind-agent-boundaries-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = self.root / 'agent.sqlite'

    def start(self, **kwargs):
        return start_agent(self.store, 'demo', 'task', SNAPSHOT, tools(), PROVIDER,
                           instructions='local test only', effect_slot='primary_record', **kwargs)

    def accept_call(self, name='read_snapshot'):
        def stop(boundary, point):
            if point == 'call_accepted':
                raise SystemExit(86)
        with patch('rewindagent.agent.create_response', return_value=response(call(name))), \
             patch.object(agent._Boundary, 'reach', stop):
            with self.assertRaises(SystemExit):
                self.start()

    def test_valid_large_budget_is_not_truncated_by_an_internal_loop_limit(self):
        responses = [response(call(call_id=f"call-{index}")) for index in range(15)]
        with patch("rewindagent.agent.create_response", side_effect=responses) as request:
            result = self.start(budgets=agent.AgentBudgets(15, 15, 15))
        self.assertEqual(result.reason_code, "budget_exhausted")
        self.assertEqual(request.call_count, 15)
        self.assertEqual(result.evidence["tool_calls"], 15)
        self.assertFalse(result.published)

    def test_uncertain_attempts_exhaust_the_saved_retry_budget(self):
        with patch.object(agent, 'create_response', return_value={'ok': False}) as request:
            first = self.start(budgets=agent.AgentBudgets(max_model_attempts=2))
            held = resume_agent(self.store, 'demo', tools(), PROVIDER)
            self.assertEqual(first.reason_code, 'model_request_unknown')
            self.assertEqual(held.reason_code, 'model_request_unknown')
            self.assertEqual(request.call_count, 1)
            retried = resume_agent(self.store, 'demo', tools(), PROVIDER, authorize_model_retry=True)
            self.assertEqual(retried.reason_code, 'model_request_unknown')
            self.assertTrue(retried.warnings)
            exhausted = resume_agent(self.store, 'demo', tools(), PROVIDER, authorize_model_retry=True)
            self.assertEqual(exhausted.reason_code, 'budget_exhausted')
            self.assertFalse(exhausted.published)
            self.assertEqual(request.call_count, 2)
        conn = connect(self.store, write=False)
        try:
            attempts = conn.execute('SELECT attempt_seq, state, response_json FROM agent_attempts ORDER BY attempt_seq').fetchall()
            self.assertEqual([tuple(row) for row in attempts], [(1, 'may_have_sent', None), (2, 'may_have_sent', None)])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_calls').fetchone()[0], 0)
        finally:
            conn.close()

    def test_resume_cli_refuses_budget_overrides_before_opening_store(self):
        from examples.responses_agent import main
        from contextlib import redirect_stderr
        from io import StringIO

        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as stopped:
            main(["resume", "--store", str(self.store), "--run-id", "demo", "--max-model-turns", "20"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("budgets are fixed at start", error.getvalue())
        self.assertFalse(self.store.exists())

    def test_coordinator_stays_locked_during_durable_handoff(self):
        lock = SessionLock(self.store)
        self.assertIsNone(lock.acquire())
        self.addCleanup(lock.release)
        lock.release_executor()
        code = ('from pathlib import Path; from rewindagent.agent_store import SessionLock; '
                'import sys; lock=SessionLock(Path(sys.argv[1])); '
                'result=lock.acquire(); lock.release(); print(result)')
        child = subprocess.run([sys.executable, '-c', code, str(self.store)],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'executor_busy')
        self.assertIsNone(lock.acquire_executor())

    def test_competing_agent_cannot_adopt_during_child_handoff(self):
        original = agent._execute_child
        competitors = []
        checked = [False]
        def probe(*args, **kwargs):
            if not checked[0]:
                checked[0] = True
                competitors.append(resume_agent(self.store, 'demo', tools(), PROVIDER))
            return original(*args, **kwargs)
        with patch('rewindagent.agent.create_response', side_effect=[response(call()), {'ok': False}]), \
             patch('rewindagent.agent._execute_child', probe):
            # The competitor must stop before entering _execute_child again.
            result = self.start()
        self.assertEqual([r.reason_code for r in competitors], ['executor_busy'])
        self.assertEqual(result.reason_code, 'model_request_unknown')
        with connect(self.store, write=False) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_items WHERE kind='function_call_output'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_executor_contention_after_child_commit_leaves_one_pending_adoption(self):
        from rewindagent.durable import _Lock
        blocker = _Lock(Path(str(self.store) + '.lock'))
        self.addCleanup(blocker.release)
        original = agent._execute_child
        def hold_executor(*args, **kwargs):
            outcome = original(*args, **kwargs)
            self.assertEqual(outcome['kind'], 'done')
            self.assertIsNone(blocker.acquire())
            return outcome
        with patch.object(agent, 'create_response', return_value=response(call())) as request, \
             patch.object(agent, '_execute_child', hold_executor):
            held = self.start()
        self.assertEqual(held.reason_code, 'executor_busy')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(query_agent(self.store, 'demo').phase, 'tool_pending')
        blocker.release()
        with patch.object(agent, 'create_response', return_value={'ok': False}):
            resumed = resume_agent(self.store, 'demo', tools(), PROVIDER)
        self.assertEqual(resumed.reason_code, 'model_request_unknown')
        conn = connect(self.store, write=False)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_items WHERE kind='function_call_output'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT child_run_id, adopted FROM agent_calls").fetchone()[:], ('demo.p1', 1))
        finally:
            conn.close()

    def _accept_effect(self):
        from rewindagent.canonical import semantic_hash
        accepted = [0]
        def stop(boundary, point):
            if point == 'call_accepted':
                accepted[0] += 1
                if accepted[0] == 2:
                    raise SystemExit(86)
        effect = call('record_summary', 'effect')
        effect['arguments'] = json.dumps({'summary_ref': semantic_hash({'count': 2, 'total': 7})})
        with patch.object(agent, 'create_response', side_effect=[response(call('summarize')), response(effect)]), \
             patch.object(agent._Boundary, 'reach', stop):
            with self.assertRaises(SystemExit):
                self.start()

    def test_damaged_pending_arguments_refuse_without_mutating_or_executing(self):
        mutations = [('arguments_json', 'broken-json'), ('arguments_digest', 'wrong'),
                     ('name', 'missing-tool'), ('child_run_id', 'unrelated.p1')]
        for index, (column, value) in enumerate(mutations):
            with self.subTest(column=column):
                self.store = self.root / f'damaged-{index}.sqlite'
                self.accept_call()
                conn = sqlite3.connect(self.store)
                try:
                    conn.execute(f'UPDATE agent_calls SET {column}=?', (value,));conn.commit()
                finally:
                    conn.close()
                before = self.store.read_bytes()
                with patch.object(agent, '_execute_child') as child, patch.object(agent, 'create_response') as request:
                    refused = resume_agent(self.store, 'demo', tools(), PROVIDER)
                self.assertEqual(refused.reason_code, 'record_corrupt')
                self.assertFalse(refused.published)
                child.assert_not_called();request.assert_not_called()
                self.assertEqual(self.store.read_bytes(), before)

    def test_reconcile_checks_saved_effect_arguments_and_child_binding(self):
        mutations = [('arguments_json', 'broken-json'), ('arguments_digest', 'wrong'),
                     ('child_run_id', 'unrelated.effect')]
        for index, (column, value) in enumerate(mutations):
            with self.subTest(column=column):
                self.store = self.root / f'reconcile-damaged-{index}.sqlite'
                self._accept_effect()
                conn = sqlite3.connect(self.store)
                try:
                    conn.execute(f"UPDATE agent_calls SET {column}=? WHERE name='record_summary'", (value,));conn.commit()
                finally:
                    conn.close()
                before = self.store.read_bytes()
                with patch.object(agent, '_execute_child') as child, patch.object(agent, 'create_response') as request:
                    refused = agent.reconcile_agent(self.store, 'demo', tools(), PROVIDER)
                self.assertEqual(refused.reason_code, 'record_corrupt')
                self.assertFalse(refused.published)
                child.assert_not_called();request.assert_not_called()
                self.assertEqual(self.store.read_bytes(), before)

    def test_preexisting_child_requires_matching_input_and_workflow(self):
        # A published durable run at the derived child name is not sufficient
        # evidence for this agent call, even with the same registered tool.
        workflow = {'schema_version': 1, 'steps': [{'step_id': 'call', 'tool': 'read_snapshot',
                    'reads': ['bound'], 'writes': ['result'], 'config': {}}]}
        tool = tools()[0]
        result = start_run(self.store, 'demo.p1', workflow, {'bound': {'records': []}},
                           [ToolSpec(tool.name, tool.fn, impl_id=tool.impl_id)])
        self.assertTrue(result.published)
        with patch('rewindagent.agent.create_response', return_value=response(call())) as request:
            refused = self.start()
        self.assertEqual(refused.reason_code, 'input_mismatch')
        self.assertFalse(refused.published)
        self.assertEqual(request.call_count, 1)

    def test_repeated_call_id_does_not_replace_an_accepted_call(self):
        for second_name in ('read_snapshot', 'summarize'):
            with self.subTest(name=second_name):
                self.store = self.root / (second_name + '.sqlite')
                with patch('rewindagent.agent.create_response', side_effect=[response(call()), response(call(second_name))]) as request:
                    refused = self.start()
                self.assertEqual(refused.reason_code, 'duplicate_call_id')
                self.assertFalse(refused.published)
                self.assertEqual(request.call_count, 2)
                conn = connect(self.store, write=False)
                try:
                    rows = conn.execute('SELECT call_id, name, adopted FROM agent_calls').fetchall()
                    self.assertEqual([tuple(row) for row in rows], [('first', 'read_snapshot', 1)])
                finally:
                    conn.close()

    def test_nonempty_pure_arguments_are_not_silently_ignored(self):
        registry = tools()
        schema = {'type': 'object', 'properties': {'amount': {'type': 'integer'}},
                  'required': ['amount'], 'additionalProperties': False}
        registry[0] = dataclasses.replace(registry[0], parameters=schema)
        with patch('rewindagent.agent.create_response') as request:
            result = start_agent(self.store, 'demo', 'task', SNAPSHOT, registry, PROVIDER,
                                 instructions='local', effect_slot='primary_record')
        self.assertEqual(result.status, 'unsupported')
        request.assert_not_called()
        self.assertFalse(self.store.exists())

    def test_reserved_store_filenames_resume_the_same_session(self):
        for name in ('run#one.sqlite', 'run?one.sqlite', 'run%23one.sqlite', '数据 空格.sqlite'):
            with self.subTest(name=name):
                self.store = self.root / name
                self.accept_call()
                viewed = query_agent(self.store, 'demo')
                self.assertEqual(viewed.phase, 'tool_pending')
                with patch('rewindagent.agent.create_response', return_value={'ok': False}) as request:
                    result = resume_agent(self.store, 'demo', tools(), PROVIDER)
                self.assertEqual(result.reason_code, 'model_request_unknown')
                self.assertEqual(request.call_count, 1)
                self.assertTrue(self.store.exists())
        self.assertFalse((self.root / 'run').exists())
        self.assertFalse((self.root / 'run#one.sqlite').samefile(self.root / 'run%23one.sqlite'))

    def test_changed_saved_snapshot_is_rejected_without_execution(self):
        self.accept_call()
        conn = sqlite3.connect(self.store)
        try:
            conn.execute('UPDATE agent_sessions SET snapshot_json=?', (json.dumps({'records': []}),))
            conn.commit()
        finally:
            conn.close()
        before = self.store.read_bytes()
        with patch('rewindagent.agent.create_response') as request:
            result = resume_agent(self.store, 'demo', tools(), PROVIDER)
        self.assertEqual(result.reason_code, 'record_corrupt')
        self.assertFalse(result.published)
        self.assertEqual(query_agent(self.store, 'demo').reason_code, 'record_corrupt')
        self.assertNotIn('items', inspect_agent(self.store, 'demo'))
        request.assert_not_called()
        self.assertEqual(self.store.read_bytes(), before)

    def test_incomplete_schema_is_read_only_refusal(self):
        self.accept_call()
        conn = sqlite3.connect(self.store)
        try:
            conn.execute('ALTER TABLE agent_calls DROP COLUMN arguments_digest');conn.commit()
        finally:
            conn.close()
        before = self.store.read_bytes()
        self.assertEqual(query_agent(self.store, 'demo').reason_code, 'schema_incomplete')
        with patch('rewindagent.agent.create_response') as request:
            self.assertEqual(resume_agent(self.store, 'demo', tools(), PROVIDER).reason_code, 'schema_incomplete')
        request.assert_not_called()
        self.assertEqual(self.store.read_bytes(), before)

    def test_corrupt_store_connection_is_closed(self):
        self.store.write_bytes(b'not a sqlite database')
        real = sqlite3.connect
        opened = []
        def track(*args, **kwargs):
            conn = real(*args, **kwargs);opened.append(conn);return conn
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            with patch('rewindagent.agent_store.sqlite3.connect', track):
                self.assertEqual(query_agent(self.store, 'demo').reason_code, 'store_corrupt')
            try:
                with self.assertRaises(sqlite3.ProgrammingError):
                    opened[0].execute('SELECT 1')
            finally:
                for conn in opened:conn.close()
                opened.clear()
            gc.collect()
            self.assertFalse([w for w in caught if issubclass(w.category, ResourceWarning)])

    def test_invalid_response_items_never_execute_a_child(self):
        bad = [call(status='in_progress'), call(call_id=''),
               {'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': None},
               {'type': 'message', 'role': 'user', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'done'}]}]
        for index, item in enumerate(bad):
            with self.subTest(item=item):
                self.store = self.root / f'bad-{index}.sqlite'
                with patch('rewindagent.agent.create_response', return_value=response(item)), \
                     patch('rewindagent.agent._execute_child') as child:
                    result = self.start()
                self.assertFalse(result.published)
                self.assertIn(result.status, ('unsupported', 'safe_refusal'))
                child.assert_not_called()

    def test_reasoning_is_preserved_for_continuation_but_redacted_in_inspect(self):
        opaque = {'type': 'reasoning', 'id': 'rs_test', 'summary': [], 'encrypted_content': 'opaque-fixture-only'}
        with patch('rewindagent.agent.create_response', side_effect=[response(opaque, call()), {'ok': False}]) as request:
            self.start()
        self.assertIn(opaque, request.call_args.kwargs['input_items'])
        self.assertNotIn('opaque-fixture-only', json.dumps(inspect_agent(self.store, 'demo')))


class EndpointBoundaryTests(unittest.TestCase):
    def test_invalid_or_nonlocal_urls_are_rejected_before_sdk_creation(self):
        for url in ('https://api.openai.com/v1', 'http://127.0.0.1:bad/v1', 'http://[broken',
                    'http://user:password@127.0.0.1:80/v1', 'http://127.0.0.1:80/v1?key=x',
                    'http://127.0.0.1:80/v1#fragment'):
            with self.subTest(url=url), patch('socket.socket') as socket:
                result = create_response(dataclasses.replace(PROVIDER, base_url=url), instructions='', input_items=[], tools=[])
            self.assertEqual(result['error'], 'endpoint_not_loopback')
            socket.assert_not_called()

    def test_nonfinite_or_boolean_timeouts_are_rejected(self):
        for timeout in (float('nan'), float('inf'), True, 0, -1, 31):
            self.assertEqual(config_problem(dataclasses.replace(PROVIDER, timeout_s=timeout)), 'timeout_out_of_range')

    def test_live_mode_never_creates_a_socket(self):
        with patch('socket.socket') as socket:
            result = create_response(dataclasses.replace(PROVIDER, mode='live', live_authorized=True),
                                     instructions='', input_items=[], tools=[])
        self.assertEqual(result['error'], 'live_not_authorized')
        socket.assert_not_called()
