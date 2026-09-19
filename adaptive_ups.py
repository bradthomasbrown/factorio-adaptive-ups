"""Adaptive UPS 0.3.0 for Factorio 2.0.73. Python 3.10+."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import socket
import statistics
import struct
import threading
import time
from access_store import AccessStore
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener


@dataclass
class Peer:
    seq: int = -1
    received: float = -math.inf
    delay: float = math.inf
    baseline: float = math.inf
    samples: deque = field(default_factory=lambda: deque(maxlen=12))


class Governor:
    """All time is supplied by caller; production uses only server monotonic time."""

    def __init__(self, floor=15.0, maximum=45.0):
        if not 0.6 <= floor <= maximum <= 60:
            raise ValueError("Require 0.6 <= floor <= maximum <= 60")
        self.floor, self.maximum = float(floor), float(maximum)
        self.target = self.floor
        self.peers: dict[str, Peer] = {}
        self.healthy_since = None
        self.last_change = -math.inf
        self.ceiling = self.maximum
        self.ceiling_until = 0.0
        self.reason = "starting at fallback"

    def roster(self, players, now):
        names = set(players)
        changed = names != set(self.peers)
        joined = names - set(self.peers)
        self.peers = {p: self.peers.get(p, Peer()) for p in names}
        if changed:
            self.healthy_since = None
        if joined or not names:
            self.target = self.floor
            self.last_change = now
        if changed:
            # A departed slow peer should not constrain the remaining group.
            self.ceiling, self.ceiling_until = self.maximum, now

    def ack(self, player, seq, sent, now):
        peer = self.peers.get(player)
        if peer is None or seq <= peer.seq or now < sent:
            return False
        peer.seq, peer.received, peer.delay = seq, now, now - sent
        peer.baseline = min(peer.baseline, peer.delay)
        peer.samples.append((now, peer.delay))
        return True

    def peer_health(self, peer, now):
        age = now - peer.received
        baseline = peer.baseline if math.isfinite(peer.baseline) else 0.0
        values = [value for _, value in peer.samples]
        recent = statistics.median(values[-3:]) if values else math.inf
        typical = statistics.median(values) if values else math.inf
        calibrated = len(values) >= 8 and peer.samples[-1][0] - peer.samples[0][0] >= 7
        growing = False
        if len(values) >= 6 and peer.samples[-1][0] - peer.samples[0][0] >= 4:
            growing = statistics.median(values[-3:]) - statistics.median(values[:3]) > 0.25
        # The baseline is the lowest observed delay and NEVER drifts upward to
        # absorb backlog. A separate absolute ceiling rejects very late joins.
        critical = age > 3.5 or peer.delay > 2.5 or peer.delay - baseline > 1.5
        warning = recent - baseline > 0.5 or (growing and recent - baseline > 0.2)
        healthy = (calibrated and age <= 2.5 and typical - baseline <= 0.3
                   and recent - baseline <= 0.35 and not growing and not critical and not warning)
        if age > 3.5:
            state = "waiting_for_reports"
        elif critical or warning:
            state = "reports_delayed"
        elif not calibrated:
            state = "calibrating"
        elif healthy:
            state = "keeping_up"
        else:
            state = "settling"
        return critical, warning, healthy, state

    def next_increase(self, now):
        if not self.peers or self.healthy_since is None or self.target >= self.maximum:
            return None
        ready = max(self.healthy_since + 15, self.last_change + 5)
        if self.target >= self.ceiling and now < self.ceiling_until:
            ready = max(ready, self.ceiling_until)
        return max(0.0, ready - now)

    def step(self, now):
        if not self.peers:
            self.target, self.reason = self.floor, "Paused while empty"
            self.healthy_since = None
            return self.target
        critical, warning, healthy = [], [], True
        for name, peer in self.peers.items():
            late, delayed, prompt, _ = self.peer_health(peer, now)
            if late:
                critical.append(name)
            if delayed:
                warning.append(name)
            healthy &= prompt
        if critical:
            if self.target > self.floor:
                self.ceiling = max(self.floor, min(self.ceiling, self.target * 0.8))
                self.ceiling_until = now + 120
            self.target, self.last_change = self.floor, now
            self.reason = "Fallback: waiting for timely reports from " + ", ".join(sorted(critical))
            self.healthy_since = None
        elif warning:
            if now - self.last_change >= 3:
                self.target = max(self.floor, round(self.target * 0.8, 2))
                self.ceiling, self.ceiling_until = self.target, now + 120
                self.last_change = now
            self.reason = "Holding or reducing speed: delayed reports from " + ", ".join(sorted(warning))
            self.healthy_since = None
        elif healthy:
            if self.healthy_since is None:
                self.healthy_since = now
            cap = self.maximum if now >= self.ceiling_until else self.ceiling
            if now - self.healthy_since >= 15 and now - self.last_change >= 5:
                self.target = min(cap, self.target + 2)
                self.last_change = now
            if self.target >= self.maximum:
                self.reason = "At the configured maximum"
            elif self.target >= cap and now < self.ceiling_until:
                self.reason = "Recovery hold after an earlier slowdown"
            else:
                self.reason = "Reports are steady; gradually increasing speed"
        else:
            self.healthy_since = None
            self.reason = "Learning normal connection delay or waiting for reports to settle"
        return self.target


class ModError(ValueError):
    def __init__(self, result):
        self.code = result.get("code")
        super().__init__(str(result.get("error", "Invalid mod response")))


class Rcon:
    """Factorio RCON: one length-prefixed response per command, matched by ID.

    Factorio uses an oversized response packet for long output rather than
    Source's multipart sentinel scheme. TCP reads may still be fragmented.
    """

    def __init__(self, host, port, password, timeout=4):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        self.counter = 10
        try:
            self.send(1, 3, password)
            while True:
                ident, kind, _ = self.receive()
                if ident == -1:
                    raise PermissionError("RCON authentication failed")
                if kind == 2:
                    if ident != 1:
                        raise ValueError("Unexpected RCON auth response")
                    break
        except Exception:
            self.close()
            raise

    def read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            part = self.sock.recv(count - len(data))
            if not part:
                raise ConnectionError("RCON closed")
            data.extend(part)
        return bytes(data)

    def send(self, ident, kind, body):
        data = struct.pack("<ii", ident, kind) + body.encode("utf-8") + b"\0\0"
        self.sock.sendall(struct.pack("<i", len(data)) + data)

    def receive(self):
        size, = struct.unpack("<i", self.read_exact(4))
        if not 10 <= size <= 4 * 1024 * 1024:
            raise ValueError("Invalid RCON packet size")
        data = self.read_exact(size)
        ident, kind = struct.unpack("<ii", data[:8])
        if data[-2:] != b"\0\0":
            raise ValueError("Invalid RCON terminator")
        return ident, kind, data[8:-2].decode("utf-8")

    def command(self, command):
        self.counter += 1
        ident = self.counter
        self.send(ident, 2, command)
        while True:
            response_id, _, body = self.receive()
            if response_id == ident:
                return body.strip()
            elif response_id < ident and not body:
                continue
            else:
                raise ValueError("Unexpected RCON response ID")

    def json_command(self, command):
        response = self.command(command)
        result = json.loads(response)
        if not isinstance(result, dict):
            raise ValueError("Mod response must be an object")
        if "error" in result:
            raise ModError(result)
        return result

    def close(self):
        self.sock.close()


class Coordinator:
    def __init__(self, governor, tokens, access_store=None):
        self.governor = governor
        self.tokens = tokens
        self.access_store = access_store
        self.session = secrets.token_hex(16)
        self.pulses = {}
        self.pending = []
        self.lock = threading.Lock()
        self.public = {"version": 1, "state": "starting", "reason": "Connecting to game server",
                       "players": [], "target_ups": governor.floor, "measured_ups": None,
                       "minimum_ups": governor.floor, "maximum_ups": governor.maximum,
                       "next_increase_seconds": None}
        self.published_at = time.monotonic()

    def pulse(self, seq, now):
        with self.lock:
            self.pulses[seq] = now
            self.pulses = {s: t for s, t in self.pulses.items() if now - t < 120}

    def accept(self, token, data, now):
        player = data.get("player")
        expected = self.tokens.get(player) if isinstance(player, str) else None
        authorized = expected and hmac.compare_digest(token.encode(), expected.encode())
        if not authorized and self.access_store:
            authorized = self.access_store.authorize(player, token)
        if not authorized:
            return 403
        with self.lock:
            seq = data.get("seq")
            if data.get("session") != self.session or type(seq) is not int or seq not in self.pulses:
                return 409
            # Defer until the pulse response supplies the current roster. This
            # also handles acknowledgements arriving before the RCON response.
            if len(self.pending) >= 4096:
                return 429
            self.pending.append((player, seq, self.pulses[seq], now))
            return 204

    def decide(self, players, now):
        with self.lock:
            self.governor.roster(players, now)
            for ack in self.pending:
                self.governor.ack(*ack)
            self.pending.clear()
            target = self.governor.step(now)
            peers = {name: {"delay_seconds": round(p.delay, 3) if math.isfinite(p.delay) else None,
                            "ack_age_seconds": round(now - p.received, 3) if math.isfinite(p.received) else None}
                     for name, p in self.governor.peers.items()}
            return target, self.governor.reason, peers


    def publish(self, state, measured_ups=None, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            g = self.governor
            if state == "idle":
                g.roster([], now)
                g.step(now)
            def finite(value):
                return round(value, 3) if math.isfinite(value) else None
            self.public = {"version": 1, "state": state,
                "reason": g.reason if state in {"running", "idle"} else "Reconnecting to game server",
                "target_ups": g.target, "measured_ups": measured_ups,
                "minimum_ups": g.floor, "maximum_ups": g.maximum,
                "next_increase_seconds": g.next_increase(now) if state == "running" else None,
                "players": [{"player": name, "state": g.peer_health(peer, now)[3],
                    "delay_seconds": finite(peer.delay), "normal_delay_seconds": finite(peer.baseline),
                    "report_age_seconds": finite(now - peer.received)}
                    for name, peer in sorted(g.peers.items())]}
            self.published_at = now

    def public_status(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            result = json.loads(json.dumps(self.public, allow_nan=False))
            age = max(0, now - self.published_at)
        result["updated_seconds_ago"] = round(age, 2)
        if age > 5:
            result.update(state="stale", reason="Waiting for fresh controller status", next_increase_seconds=None)
        elif result["next_increase_seconds"] is not None:
            result["next_increase_seconds"] = round(max(0, result["next_increase_seconds"] - age), 1)
        return result


def make_handler(coordinator):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path != "/status":
                self.send_error(404)
                return
            body = json.dumps(coordinator.public_status(), allow_nan=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/ack" or not 0 < size <= 4096:
                    self.send_error(400)
                    return
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Object required")
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                code = coordinator.accept(token, data, time.monotonic())
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (ValueError, UnicodeError):
                self.send_error(400)
            except (OSError, TimeoutError):
                self.close_connection = True
    return Handler


def emit(**event):
    print(json.dumps({"time": time.time(), **event}, allow_nan=False), flush=True)


def run_server(args):
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    tokens = config.get("client_tokens", {})
    if not tokens or any(not isinstance(v, str) or len(v) < 24 for v in tokens.values()):
        raise ValueError("Configure a unique random client token (24+ characters) for each player")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("Each player needs a different token")
    password = os.environ[config.get("rcon_password_env", "FACTORIO_RCON_PASSWORD")]
    gov = Governor(config.get("floor_ups", 15), config.get("maximum_ups", 45))
    store = AccessStore(config['access_database']) if config.get('access_database') else None
    coordinator = Coordinator(gov, tokens, store)
    http = ThreadingHTTPServer((config.get("listen_host", "127.0.0.1"), config.get("listen_port", 8765)), make_handler(coordinator))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    rcon, seq, previous_sample = None, 0, None
    claimed = False
    roster_path = Path(config['server_roster_file']) if config.get('server_roster_file') else None
    try:
        while True:
            start = time.monotonic()
            try:
                if roster_path is not None and (claimed or not args.control):
                    try:
                        with roster_path.open('rb') as stream:
                            roster = json.loads(stream.read(4097))
                    except (OSError, ValueError):
                        roster = None
                    if isinstance(roster, dict):
                        if args.control and roster.get('enabled') and roster.get('owner') != coordinator.session:
                            raise ModError({'error': 'Another controller owns adaptive mode', 'code': 'owner_mismatch'})
                        if args.control and not roster.get('enabled'):
                            emit(event='disarmed', message='Operator disabled mod; controller exiting')
                            break
                        if not roster.get('players'):
                            coordinator.publish("idle", now=start)
                            previous_sample = None
                            emit(event='idle', reason='empty; waiting for join event without advancing simulation')
                            time.sleep(1)
                            continue
                if rcon is None:
                    rcon = Rcon(config.get("rcon_host", "127.0.0.1"), config.get("rcon_port", 27015), password)
                    if args.control and not claimed:
                        rcon.json_command(f"/adaptive-ups-enable {coordinator.session} {gov.floor:g}")
                        claimed = True
                        gov.target = gov.floor
                        gov.healthy_since = None
                    emit(event="connected", mode="control" if args.control else "observe")
                seq += 1
                status = None if args.control else rcon.json_command("/adaptive-ups-status")
                passive = status is not None and status.get("enabled")
                if passive:
                    result = status
                else:
                    coordinator.pulse(seq, time.monotonic())
                    result = rcon.json_command(f"/adaptive-ups-pulse {coordinator.session} {seq}")
                sample_time = time.monotonic()
                measured_ups = None
                if previous_sample and result["tick"] >= previous_sample[0]:
                    measured_ups = round((result["tick"] - previous_sample[0]) / (sample_time - previous_sample[1]), 2)
                previous_sample = (result["tick"], sample_time)
                if args.control and float(result["target_ups"]) < gov.target - 0.01:
                    # Respect the mod's immediate join/watchdog reduction, even
                    # if a leave and rejoin occurred between two roster polls.
                    gov.target = max(gov.floor, float(result["target_ups"]))
                    gov.healthy_since, gov.last_change = None, time.monotonic()
                if passive:
                    target, reason, peers = result["target_ups"], "read-only observer; active controller owns markers", {}
                else:
                    target, reason, peers = coordinator.decide(result.get("players", []), time.monotonic())
                if args.control and not result.get("enabled"):
                    emit(event="disarmed", message="Operator disabled mod; controller exiting")
                    break
                if args.control and abs(float(result["target_ups"]) - target) > 0.01:
                    rcon.json_command(f"/adaptive-ups-speed {coordinator.session} {target:g}")
                coordinator.publish("running", measured_ups, sample_time)
                emit(event="sample", seq=seq, server_tick=result["tick"], observed_target_ups=result["target_ups"],
                     measured_server_ups=measured_ups, proposed_target_ups=target, reason=reason, peers=peers)
            except (OSError, ValueError, ConnectionError, PermissionError) as exc:
                emit(event="connection_error", error=str(exc))
                if isinstance(exc, ModError) and exc.code == "owner_mismatch":
                    emit(event="ownership_lost", message="Controller exiting; explicit restart is required to claim control")
                    raise SystemExit(2)
                if rcon is not None:
                    rcon.close()
                    rcon = None
                # The deterministic mod watchdog supplies the fallback while
                # RCON is inaccessible. Never silently resume at full speed.
                gov.target, gov.healthy_since = gov.floor, None
                previous_sample = None
                coordinator.publish("reconnecting")
                time.sleep(2)
            time.sleep(max(0, 1 - (time.monotonic() - start)))
    except KeyboardInterrupt:
        emit(event="stopping", message="Armed watchdog will return to fallback")
    finally:
        if rcon is not None:
            rcon.close()
        http.shutdown()
        http.server_close()


def run_client(args):
    parsed = urlparse(args.server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) server URL without embedded credentials")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and not args.allow_http:
        raise ValueError("Use HTTPS, or --allow-http only over a trusted private network/VPN")
    token = os.environ[args.token_env]
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *unused, **kwargs):
            return None
    opener = build_opener(NoRedirect())
    path = Path(args.beacon)
    last = None
    last_notice = 0.0
    emit(event="watching", path=str(path), player=args.player)
    try:
        while True:
            try:
                raw = path.read_bytes()
                if len(raw) > 4096:
                    raise ValueError("Oversized marker")
                b = json.loads(raw)
                if not isinstance(b, dict):
                    raise ValueError("Marker must be an object")
                if b.get("version") != 1 or b.get("player") != args.player:
                    raise ValueError("Marker identity does not match this player")
                key = (b["session"], b["seq"])
                if key != last:
                    data = json.dumps({"player": args.player, "session": b["session"], "seq": b["seq"]}).encode()
                    req = Request(args.server.rstrip("/") + "/ack", data=data,
                                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                    with opener.open(req, timeout=2) as response:
                        if response.status != 204:
                            raise ValueError("Unexpected acknowledgement response")
                    last = key
                    if args.log_acks:
                        emit(event="acknowledged", session=b["session"], seq=b["seq"], client_tick=b.get("tick"),
                             monotonic_seconds=time.monotonic())
            except (OSError, ValueError, KeyError, TypeError, URLError, HTTPError) as exc:
                now = time.monotonic()
                if now - last_notice >= 5:
                    emit(event="waiting", detail=str(exc))
                    last_notice = now
                time.sleep(0.2)
            time.sleep(0.05)
    except KeyboardInterrupt:
        emit(event="stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    server = sub.add_parser("server")
    server.add_argument("--config", required=True)
    server.add_argument("--control", action="store_true", help="Enable actual speed changes; default only observes")
    server.set_defaults(run=run_server)
    client = sub.add_parser("client")
    client.add_argument("--beacon", required=True, help="Factorio script-output/adaptive-ups/beacon.json")
    client.add_argument("--player", required=True, help="Exact Factorio player name")
    client.add_argument("--server", required=True, help="Coordinator URL, without /ack")
    client.add_argument("--token-env", default="ADAPTIVE_UPS_TOKEN")
    client.add_argument("--allow-http", action="store_true")
    client.add_argument("--log-acks", action="store_true", help="Log executed client markers for a controlled experiment")
    client.set_defaults(run=run_client)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
