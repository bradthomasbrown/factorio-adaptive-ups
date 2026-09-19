from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_ups import Coordinator, Governor, make_handler


class CompanionTransportTests(unittest.TestCase):
    def test_real_companion_process_recovers_from_partial_file_and_posts_ack(self):
        # Synthetic marker, real file watcher / HTTP / authentication / governor.
        # This does NOT assert that a graphical Factorio peer emitted the file.
        token = secrets.token_hex(32)
        c = Coordinator(Governor(), {"test-player": token})
        http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(c))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as temp:
                marker = Path(temp) / 'beacon.json'
                marker.write_text('{"version":')
                env = dict(os.environ, ADAPTIVE_UPS_TOKEN=token)
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / 'adaptive_ups.py'),
                    'client', '--player', 'test-player', '--beacon', str(marker),
                    '--server', f'http://127.0.0.1:{http.server_address[1]}'], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                time.sleep(0.3)
                now = time.monotonic()
                c.pulse(1, now)
                marker.write_text(json.dumps({'version': 1, 'player': 'test-player', 'session': c.session, 'seq': 1}))
                deadline = now + 5
                while time.monotonic() < deadline:
                    c.decide(['test-player'], time.monotonic())
                    if c.governor.peers['test-player'].seq == 1:
                        break
                    time.sleep(0.05)
                self.assertEqual(c.governor.peers['test-player'].seq, 1)
                self.assertIsNone(process.poll())
                self.assertLess(c.governor.peers['test-player'].delay, 2)
                process.terminate()
                process.communicate(timeout=5)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
            http.shutdown()
            http.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
