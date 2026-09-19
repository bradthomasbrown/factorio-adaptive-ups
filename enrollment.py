"""Same-page, open enrollment. Never grants game administrator or RCON access."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from access_store import AccessStore, EnrollmentLimit

PLAYER = re.compile(r'[A-Za-z0-9_.-]{1,64}')


class Enrollment:
    def __init__(self, store, origin):
        self.store, self.origin = store, origin

    def create(self, player, address):
        if not isinstance(player, str) or not PLAYER.fullmatch(player):
            raise ValueError('Enter your exact Factorio account name: letters, numbers, underscore, dot or hyphen.')
        token = secrets.token_urlsafe(32)
        self.store.issue(player, token, address)
        return {'version': 1, 'server': self.origin, 'player': player, 'token': token}


def make_handler(enrollment):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AdaptiveUPS'

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Neither credentials nor client addresses go into access logs.

        def respond(self, code, value, filename=None):
            body = (json.dumps(value) + '\n').encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            if code == 429:
                self.send_header('Retry-After', '60')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024:
                    raise ValueError('Invalid request size.')
                # Drain the bounded body before an error response, avoiding TCP
                # resets when the client has already sent bytes we reject.
                raw = self.rfile.read(size)
                if self.path != '/join':
                    return self.respond(404, {'error': 'Not found.'})
                if self.headers.get('Origin') != enrollment.origin:
                    return self.respond(403, {'error': 'Use the Download my access file button on the setup page.'})
                if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    return self.respond(400, {'error': 'Use the setup page to create your access file.'})
                # Caddy overwrites this header; the service only binds to loopback.
                address = ipaddress.ip_address(self.headers.get('X-Enrollment-IP', self.client_address[0]))
                if address.version == 6:
                    address = ipaddress.ip_network(f'{address}/64', strict=False).network_address
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('Enter your Factorio account name.')
                profile = enrollment.create(data.get('player'), str(address))
                self.respond(201, profile, f'adaptive-ups-{profile["player"]}.json')
            except EnrollmentLimit as exc:
                self.respond(429, {'error': str(exc)})
            except (ValueError, UnicodeError):
                self.respond(400, {'error': 'Enter your exact Factorio account name (letters, numbers, underscore, dot or hyphen). Maximum 64 characters.'})
            except (OSError, sqlite3.Error, RuntimeError):
                self.respond(503, {'error': 'Access-file downloads are temporarily unavailable. Please try again shortly.'})
    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    origin = config['public_origin']
    store = AccessStore(config['access_database'])
    store.initialize()
    http = BoundedServer(('127.0.0.1', 8766), make_handler(Enrollment(store, origin)))
    print('Self-service access-file downloads listening on loopback:8766', flush=True)
    try:
        http.serve_forever()
    finally:
        http.server_close()


if __name__ == '__main__':
    main()
