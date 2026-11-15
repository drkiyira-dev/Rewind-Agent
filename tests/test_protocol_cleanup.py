"""The local protocol fixture owns its sockets and failed SQLite connections."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rewindagent.protocolsvc import _Server, serve

SCRIPT = {'source': 'synthetic_fixture_not_a_live_model', 'model': 'fixture-responses-1', 'responses': []}


class ProtocolCleanupTests(unittest.TestCase):
    def test_initialization_failure_closes_connection_and_listening_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.sqlite';path.write_bytes(b'not sqlite')
            connections, servers = [], []
            real_connect, real_close = sqlite3.connect, _Server.server_close
            def connect(*args, **kwargs):
                conn = real_connect(*args, **kwargs);connections.append(conn);return conn
            def close(server):
                servers.append(server);real_close(server)
            try:
                with patch('rewindagent.protocolsvc.sqlite3.connect', connect), patch.object(_Server, 'server_close', close):
                    with self.assertRaises(sqlite3.DatabaseError):_Server(path, SCRIPT)
                self.assertEqual(len(servers), 1)
                self.assertEqual(servers[0].socket.fileno(), -1)
                with self.assertRaises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')
                self.assertEqual(path.read_bytes(), b'not sqlite')
            finally:
                for conn in connections:conn.close()
                for server in servers:real_close(server)

    def test_ready_failure_and_interruption_close_server(self):
        for invalid_ready in (True, False):
            with self.subTest(invalid_ready=invalid_ready), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);fixture = root / 'fixture.json';fixture.write_text(json.dumps(SCRIPT))
                ready = root / 'missing/ready.json' if invalid_ready else root / 'ready.json'
                servers = [];real_close = _Server.server_close
                def close(server):servers.append(server);real_close(server)
                try:
                    with patch.object(_Server, 'server_close', close), patch.object(_Server, 'serve_forever', side_effect=KeyboardInterrupt):
                        with self.assertRaises(FileNotFoundError if invalid_ready else KeyboardInterrupt):
                            serve(root / 'fixture.sqlite', fixture, ready)
                    self.assertEqual(len(servers), 1)
                    self.assertEqual(servers[0].socket.fileno(), -1)
                finally:
                    for server in servers:real_close(server)
