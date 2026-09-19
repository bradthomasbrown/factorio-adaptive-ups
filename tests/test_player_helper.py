from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_helper import (install_mod, MOD_FILE, validate_profile, watch_progress,
    detect_player, find_data_directories, get_or_create_profile, save_json,
    Connection, ServerResponse, format_status)


class HelperTests(unittest.TestCase):
    def test_profile_rejects_insecure_remote_and_embedded_credentials(self):
        base = {"version": 1, "player": "test", "token": "a" * 32, "server": "https://example.com"}
        self.assertEqual(validate_profile(base), base)
        for url in ("http://example.com", "https://user:password@example.com", "file:///test", "https://example.com/?token=secret"):
            with self.assertRaises(ValueError):
                validate_profile({**base, "server": url})

    def test_mod_install_preserves_other_mods_and_backs_up_different_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, target = root / "source.zip", root / "mods"
            source.write_bytes(b"new custom mod")
            target.mkdir()
            (target / "unrelated.zip").write_bytes(b"other mod")
            (target / MOD_FILE).write_bytes(b"existing custom mod")
            install_mod(source, target)
            self.assertEqual((target / MOD_FILE).read_bytes(), b"new custom mod")
            self.assertEqual((target / "unrelated.zip").read_bytes(), b"other mod")
            backups = list(target.glob("*.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"existing custom mod")
            before = (target / MOD_FILE).stat().st_mtime_ns
            install_mod(source, target)
            self.assertEqual((target / MOD_FILE).stat().st_mtime_ns, before)
            self.assertEqual(len(list(target.glob("*.backup-*"))), 1)

    def test_account_detection_returns_name_only_and_lists_ambiguous_installs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            roaming, portable = root / 'roaming/Factorio', root / 'factorio-portable/Factorio_2.0.73'
            for folder in (roaming, portable):
                folder.mkdir(parents=True)
                (folder / 'player-data.json').write_text(json.dumps({'service-username': 'alice', 'service-token': 'never-send-this'}))
                self.assertEqual(detect_player(folder), 'alice')
            self.assertEqual(set(find_data_directories(root, root / 'roaming')), {str(roaming.resolve()), str(portable.resolve())})

    def test_automatic_access_is_reused_and_acks_reuse_the_connection(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path, body, self.client_address, self.headers.get('Origin')))
                result = {'version': 1, 'player': body['player'], 'token': 'x' * 32, 'server': origin}
                raw = json.dumps(result).encode() if self.path == '/join' else b''
                self.send_response(201 if self.path == '/join' else 204)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        origin = f'http://127.0.0.1:{server.server_port}'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'profile.json'
                profile = get_or_create_profile(path, 'alice', origin, allow_local=True)
                save_json(path, profile)
                self.assertEqual(get_or_create_profile(path, 'alice', origin, allow_local=True), profile)
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0][1], {'player': 'alice'})
                self.assertEqual(received[0][3], origin)
                conn = Connection(origin, allow_local=True)
                try:
                    for seq in (1, 2):
                        conn.request('POST', '/ack', {'player': 'alice', 'seq': seq}, profile['token'])
                    self.assertEqual(received[1][2], received[2][2])
                finally:
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_redirect_does_not_forward_credentials(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/collect-secret')
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = Connection(f'http://127.0.0.1:{server.server_port}', allow_local=True)
        try:
            with self.assertRaises(ServerResponse):
                conn.request('POST', '/ack', {}, 'private-token')
            self.assertEqual(paths, ['/ack'])
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

    def test_status_distinguishes_measured_target_stale_and_conditional_countdown(self):
        value = {'state': 'running', 'measured_ups': 23, 'target_ups': 25, 'maximum_ups': 25,
                 'reason': 'Holding', 'next_increase_seconds': None, 'players': []}
        title, detail, _ = format_status(value)
        self.assertIn('23.0 measured', title)
        self.assertIn('25.0 target', title)
        self.assertNotIn('increase', detail)
        self.assertIn('if reports stay timely', format_status({**value, 'next_increase_seconds': 4})[1])
        self.assertNotIn('23.0', format_status({**value, 'state': 'stale'})[0])
        self.assertIn('Paused', format_status({'state': 'idle'})[0])

    def test_watcher_recovers_and_reports_acknowledged_progress(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(204)
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stop = threading.Event()
        messages = []
        worker = None
        try:
            with tempfile.TemporaryDirectory() as d:
                marker = Path(d) / "script-output/adaptive-ups/beacon.json"
                marker.parent.mkdir(parents=True)
                marker.write_text('{"version":')
                profile = {"version": 1, "player": "test", "token": "b" * 32,
                           "server": f"http://127.0.0.1:{server.server_port}"}
                worker = threading.Thread(target=watch_progress, args=(profile, d, stop, lambda *v: messages.append(v)),
                                          kwargs={"allow_local": True}, daemon=True)
                worker.start()
                time.sleep(0.2)
                marker.write_text(json.dumps({"version": 1, "player": "test", "session": "c" * 32, "seq": 7}))
                deadline = time.monotonic() + 4
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(received)
                self.assertEqual(received[0][0], "Bearer " + "b" * 32)
                self.assertEqual(received[0][1]["seq"], 7)
                time.sleep(0.2)
                self.assertEqual(len(received), 1)
                self.assertTrue(any(v[0].startswith("Connected") for v in messages))
        finally:
            stop.set()
            if worker:
                worker.join(timeout=3)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
