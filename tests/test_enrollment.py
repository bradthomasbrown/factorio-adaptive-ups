from http.client import HTTPConnection
from pathlib import Path
import json
import secrets
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from access_store import AccessStore, EnrollmentLimit
from adaptive_ups import Coordinator, Governor
from enrollment import BoundedServer, Enrollment, make_handler
from player_helper import validate_profile


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'access.sqlite3'
        self.store = AccessStore(self.path)
        self.store.initialize()
        self.origin = 'https://94.16.31.89'
        self.enrollment = Enrollment(self.store, self.origin)

    def tearDown(self):
        self.temp.cleanup()

    def test_downloads_survive_restart_without_rotating_old_credentials(self):
        old = secrets.token_urlsafe(32)
        coordinator = Coordinator(Governor(), {'alice': old}, self.store)
        first = self.enrollment.create('alice', '127.0.0.1')
        second = Enrollment(AccessStore(self.path), self.origin).create('alice', '127.0.0.1')
        self.assertNotEqual(first['token'], second['token'])
        for profile in (first, second):
            self.assertEqual(validate_profile(profile), profile)
            self.assertNotIn(profile['token'].encode(), self.path.read_bytes())
        coordinator.pulse(7, 10)
        marker = {'player': 'alice', 'session': coordinator.session, 'seq': 7}
        for token in (old, first['token'], second['token']):
            self.assertEqual(coordinator.accept(token, marker, 10.1), 204)
        self.assertEqual(coordinator.accept(first['token'], {**marker, 'player': 'bob'}, 10.1), 403)
        self.assertEqual(coordinator.accept(first['token'], {**marker, 'session': 'wrong'}, 10.1), 409)
        coordinator.decide(['alice'], 10.1)
        self.assertEqual(coordinator.governor.peers['alice'].seq, 7)
        self.store.revoke('alice')
        self.assertEqual(coordinator.accept(first['token'], marker, 10.1), 403)
        self.assertEqual(coordinator.accept(old, marker, 10.1), 204)

    def test_invalid_names_never_create_a_profile(self):
        for value in ('', '../alice', 'alice\n/ban bob', 'a' * 65, {'player': 'alice'}, 3, None):
            with self.assertRaises(ValueError):
                self.enrollment.create(value, '127.0.0.1')

    def test_rate_limits_persist_and_expire_without_revoking_profiles(self):
        tokens = [secrets.token_urlsafe(32) for _ in range(7)]
        for token in tokens[:6]:
            self.store.issue('alice', token, '127.0.0.1', now=1000)
        with self.assertRaises(EnrollmentLimit):
            AccessStore(self.path).issue('alice', tokens[6], '127.0.0.1', now=1010)
        self.store.issue('alice', tokens[6], '127.0.0.1', now=1061)
        self.assertTrue(self.store.authorize('alice', tokens[0]))

    def test_http_profile_attachment_and_error_responses(self):
        server = BoundedServer(('127.0.0.1', 0), make_handler(self.enrollment))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(value, origin=self.origin, content_type='application/json'):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                connection.request('POST', '/join', json.dumps(value),
                    {'Origin': origin, 'Content-Type': content_type, 'X-Enrollment-IP': '127.0.0.1'})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), json.loads(response.read())
            finally:
                connection.close()
        try:
            status, headers, profile = post({'player': 'alice'})
            self.assertEqual(status, 201)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertIn('adaptive-ups-alice.json', headers['Content-Disposition'])
            self.assertEqual(validate_profile(profile), profile)
            self.assertTrue(self.store.authorize('alice', profile['token']))
            self.assertEqual(post({'player': 'bob'}, 'https://other.example')[0], 403)
            self.assertEqual(post({'player': 'bob'}, content_type='text/plain')[0], 400)
            self.assertEqual(post({'player': '../bob'})[0], 400)
            for _ in range(5):
                self.assertEqual(post({'player': 'alice'})[0], 201)
            self.assertEqual(post({'player': 'alice'})[0], 429)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
