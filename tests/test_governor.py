import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_ups import Coordinator, Governor


class GovernorTests(unittest.TestCase):
    def healthy(self, governor, start, stop, players=("fast",)):
        governor.roster(players, start)
        for i in range(int(start), int(stop)):
            for name in players:
                governor.ack(name, i, i, i + 0.15)
            governor.step(i + 0.2)

    def test_healthy_client_gradually_reaches_configured_ceiling(self):
        g = Governor()
        self.healthy(g, 0, 100)
        self.assertEqual(g.target, 45)

    def test_unknown_join_immediately_lowers_speed(self):
        g = Governor()
        self.healthy(g, 0, 100)
        g.roster(["fast", "new"], 100)
        self.assertEqual(g.step(100), 15)
        self.healthy(g, 101, 110, ("fast", "new"))
        self.assertEqual(g.target, 15)

    def test_missing_companion_never_counts_as_healthy(self):
        g = Governor()
        self.healthy(g, 0, 100)
        self.assertEqual(g.step(105), 15)

    def test_old_or_duplicate_ack_cannot_refresh_health(self):
        g = Governor()
        g.roster(["fast"], 0)
        self.assertTrue(g.ack("fast", 2, 0, 0.1))
        self.assertFalse(g.ack("fast", 1, 0, 3))
        self.assertFalse(g.ack("fast", 2, 0, 3))
        self.assertEqual(g.peers["fast"].received, 0.1)

    def test_growing_delay_reduces_and_holds_speed(self):
        g = Governor()
        self.healthy(g, 0, 100)
        for i in range(100, 104):
            g.ack("fast", i, i - (i - 99) * 0.15, i + 0.15)
            g.step(i + 0.2)
        self.assertLess(g.target, 45)
        cap = g.target
        self.healthy(g, 104, 150)
        self.assertLessEqual(g.target, cap)

    def test_single_small_jitter_does_not_drop_speed(self):
        g = Governor()
        self.healthy(g, 0, 100)
        g.ack("fast", 100, 99.8, 100.15)
        self.assertEqual(g.step(100.2), 45)

    def test_large_delay_falls_back_without_waiting_for_smoothing(self):
        g = Governor()
        self.healthy(g, 0, 100)
        g.ack("fast", 100, 98, 100.5)
        self.assertEqual(g.step(100.6), 15)

    def test_departed_slow_peer_no_longer_limits_group(self):
        g = Governor()
        g.roster(["fast", "slow"], 0)
        self.healthy(g, 1, 120, ("fast",))
        self.assertEqual(g.target, 45)

    def test_coordinator_auth_session_sequence_and_roster_race(self):
        g = Governor()
        c = Coordinator(g, {"fast": "x" * 32})
        c.pulse(1, 0)
        payload = {"player": "fast", "session": c.session, "seq": 1}
        self.assertEqual(c.accept("wrong", payload, 0.1), 403)
        self.assertEqual(c.accept("x" * 32, {**payload, "session": "old"}, 0.1), 409)
        self.assertEqual(c.accept("x" * 32, {**payload, "seq": 999}, 0.1), 409)
        self.assertEqual(c.accept("x" * 32, payload, 0.1), 204)
        c.decide(["fast"], 0.2)
        self.assertEqual(g.peers["fast"].seq, 1)


    def test_steady_internet_latency_and_jitter_can_reach_maximum(self):
        g = Governor()
        g.roster(["remote"], 0)
        for i in range(120):
            delay = (0.95, 1.05, 1.12, 1.01, 1.18)[i % 5]
            g.ack("remote", i, i, i + delay)
            g.step(i + 1.2)
        self.assertEqual(g.target, 45)
        self.assertAlmostEqual(g.peers['remote'].baseline, 0.95)
        g.ack('remote', 121, 121, 122.8)
        self.assertEqual(g.step(122.9), 45, 'One modest jitter spike should not erase healthy history')

    def test_slowly_growing_backlog_never_becomes_normal_delay(self):
        g = Governor()
        g.roster(['remote'], 0)
        for i in range(100):
            g.ack('remote', i, i, i + 1.0)
            g.step(i + 1.1)
        self.assertEqual(g.target, 45)
        for i in range(100, 200):
            delay = 1.0 + (i - 99) * 0.015
            g.ack('remote', i, i, i + delay)
            g.step(i + delay + 0.1)
        self.assertEqual(g.peers['remote'].baseline, 1.0)
        self.assertEqual(g.target, 15)

    def test_already_large_join_backlog_is_not_accepted_as_normal(self):
        g = Governor()
        g.roster(['remote'], 0)
        for i in range(100):
            g.ack('remote', i, i, i + 4)
            g.step(i + 4.1)
        self.assertEqual(g.target, 15)
        self.assertIsNone(g.next_increase(105))

    def test_no_increase_countdown_without_healthy_reports_or_at_maximum(self):
        g = Governor()
        g.roster(['remote'], 0)
        self.assertIsNone(g.next_increase(0))
        self.healthy(g, 1, 12, ('remote',))
        self.assertGreater(g.next_increase(12), 0)
        self.healthy(g, 12, 120, ('remote',))
        self.assertEqual(g.target, 45)
        self.assertIsNone(g.next_increase(120))

    def test_public_status_contains_no_credentials_and_expires_without_game_polling(self):
        c = Coordinator(Governor(), {'remote': 'secret-token-for-test'})
        c.governor.roster(['remote'], 0)
        c.publish('running', 15, now=1)
        status = c.public_status(now=2)
        text = json.dumps(status)
        self.assertNotIn(c.session, text)
        self.assertNotIn('secret-token-for-test', text)
        self.assertEqual(status['players'][0]['state'], 'waiting_for_reports')
        self.assertEqual(c.public_status(now=7)['state'], 'stale')
        c.publish('idle', now=8)
        self.assertEqual(c.public_status(now=8)['players'], [])

    def test_invalid_bounds(self):
        for floor, maximum in [(0, 45), (30, 15), (15, 61), (float("nan"), 45)]:
            with self.assertRaises(ValueError):
                Governor(floor, maximum)


def simulate(capacity_before, capacity_after, change_at=110, duration=400, floor=15, maximum=45, **timings):
    """A tick queue, not a UPS sensor: only executed pulse acknowledgements feed back.

    Client has unlimited catch-up scheduling up to its supplied capacity. This
    models the proposed signal; it is not a model of Factorio's frame scheduler.
    """
    g = Governor(floor, maximum, **timings)
    g.roster(["player"], 0)
    server_tick = client_tick = 0.0
    queued = []
    sequence = 0
    rows = []
    for step in range(duration * 20):
        now = step / 20
        capacity = capacity_before if now < change_at else capacity_after
        server_tick += g.target * 0.05
        client_tick = min(server_tick, client_tick + capacity * 0.05)
        if step % 20 == 0:
            sequence += 1
            queued.append((sequence, now, server_tick))
        while queued and client_tick >= queued[0][2]:
            seq, sent, tick = queued.pop(0)
            g.ack("player", seq, sent, now + 0.1)
        if step % 20 == 4:
            g.step(now)
        if step % 20 == 19:
            rows.append({"second": round(now, 2), "capacity": capacity, "target": g.target,
                         "backlog_ticks": round(server_tick - client_tick, 2)})
    return rows


class ClosedLoopTests(unittest.TestCase):
    def test_fast_profile_reaches_60_but_still_recovers_after_capacity_drop(self):
        rows = simulate(70, 20, maximum=60, healthy_seconds=1.5, increase_interval=0.5, recovery_seconds=12)
        self.assertEqual(rows[100]['target'], 60)
        self.assertLess(max(r['backlog_ticks'] for r in rows), 180)
        self.assertTrue(any(r['backlog_ticks'] == 0 for r in rows[120:145]))
        self.assertLessEqual(max(r['backlog_ticks'] for r in rows[-60:]), 60)

    def test_fast_profile_does_not_skip_initial_calibration(self):
        g = Governor(15, 60, healthy_seconds=1.5, increase_interval=0.5, recovery_seconds=12)
        g.roster(['player'], 0)
        for second in range(1, 7):
            g.ack('player', second, second, second + 0.2)
            g.step(second + 0.3)
        self.assertEqual(g.target, 15)

    def test_invalid_tuning_is_rejected(self):
        for value in (0, -1, float('nan'), float('inf'), 'fast'):
            with self.assertRaises(ValueError):
                Governor(15, 60, recovery_seconds=value)

    def test_sudden_capacity_drop_clears_backlog(self):
        rows = simulate(50, 20)
        self.assertEqual(rows[100]["target"], 45)
        self.assertLess(max(r["backlog_ticks"] for r in rows), 150)
        self.assertEqual(rows[-1]["backlog_ticks"], 0)
        self.assertLessEqual(rows[-1]["target"], 20)

    def test_hard_floor_cannot_support_a_client_below_floor(self):
        rows = simulate(50, 10, duration=200)
        self.assertEqual(rows[-1]["target"], 15)
        self.assertGreater(rows[-1]["backlog_ticks"], 400)

    def test_lower_fallback_can_recover_same_slow_client(self):
        rows = simulate(50, 10, duration=300, floor=8)
        self.assertEqual(rows[-1]["backlog_ticks"], 0)

    def test_capacity_equal_to_floor_cannot_drain_existing_backlog(self):
        rows = simulate(50, 15, duration=200)
        self.assertEqual(rows[-1]["target"], 15)
        self.assertGreater(rows[-1]["backlog_ticks"], 0)


if __name__ == "__main__":
    unittest.main()
