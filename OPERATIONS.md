# Netcup operations

SSH: `root@94.16.31.89`, port 22. Use your authorized SSH key. Keys are not included in this release. Keep player profiles and `/etc/factorio/rcon.env`
private; never upload them to `/srv/factorio-web`.

## Routine commands (inside SSH)

```sh
systemctl status factorio adaptive-ups factorio-access factorio-backup.timer --no-pager
factorio-admin status
factorio-admin backup
factorio-admin fixed 15
factorio-admin adaptive
journalctl -u adaptive-ups -n 30 --no-pager
```

`fixed 15` stops the controller and disarms its watchdog at 15 UPS. It lasts until
adaptive mode is explicitly resumed or Factorio is restarted. `adaptive` restarts
the controller at fallback. To change the normal range, edit `floor_ups` and
`maximum_ups` in `/etc/factorio/adaptive.json`, then restart `adaptive-ups`.
The current experimental range is 15–60. Lower the floor if a player cannot catch
up at 15. Raising the maximum does not make the hardware compute faster.

Controller 0.3.1 accepts these additional settings in the same private JSON file:

| Setting | Default when omitted | Current trial |
|---|---:|---:|
| `healthy_seconds` | 15 | 1.5 |
| `increase_interval` | 5 | 0.5 |
| `recovery_seconds` | 120 | 12 |

Values must be finite numbers from 0.1 to 600 seconds. The controller runs about
once per second, so a 0.5-second increase interval still means at most one +2 UPS
step per loop. Initial calibration still requires at least eight acknowledgments
spanning seven seconds; the twelve-sample window and late-report safeguards are
unchanged. A recovery countdown begins only once reports permit an increase; it
is not a promise to speed up twelve seconds after a slowdown. See [trial evidence](TUNING.md).

To return to the previous conservative settings, set the maximum to 25 and remove
the three timing keys (or set 15, 5, 120), then restart only `adaptive-ups`.
The game does not need to restart. Keep a root-only configuration backup: this
file contains reporting credentials and must never be published.

The mod watchdog returns to fallback after 180–210 simulation ticks without an
owner pulse; at 15 UPS this is at most 14 seconds. It does not run during a pause.
Automatic service restart handles controller crashes. Loss of controller ownership
exits with code 2 and requires an explicit restart, preventing takeover loops.

## Player access files

The 0.3.0 companion obtains reporting access automatically through HTTPS `/join`.
The setup page https://94.16.31.89/ is open enrollment: no invite secret, whitelist or host approval. Factorio still
verifies accounts; only `punchyfist` is an administrator. The web form does not
verify ownership of the entered name and never asks for a Factorio password.
Profiles authorize progress reporting only; current game markers are also needed.

`factorio-access.service` runs as a separate unprivileged account, with no RCON
credential or game-file access. It listens on loopback 8766 behind Caddy `/join`.
Configuration is `/etc/factorio-access.json`. The private registry is
`/var/lib/factorio-access/profiles.sqlite3`, storing token hashes, not tokens.
The controller reads it without a restart. Downloads use `Cache-Control: no-store`;
credentials are never placed in URLs, page HTML, logs or the public downloads folder.
Issuing another file leaves earlier files valid, including preexisting host profiles.

Limits: 6 downloads/minute and 20/hour per address (IPv6 grouped by /64), 120/hour
across the site, and 10,000 saved profiles. Caddy overwrites the client-address
header. Hourly rate-limit records are pruned when another download is requested.
If a legitimate group reaches these limits, adjust `access_store.py` and restart
`factorio-access`; do not clear other players’ profiles casually.

For an operator-only credential reset, `factorio-admin enroll ExactAccountName`
revokes that player’s web profiles, rotates their older configured token and writes
a private replacement to `/etc/factorio/profiles/ExactAccountName.json`, then restarts
the controller. Normal onboarding does not need this command. Since the page is
open, it can issue new files afterward. Use Factorio’s normal ban command to exclude
a player from the game.

## Backups and recovery

A systemd timer makes a checkpoint every ten **wall-clock** minutes, independent
of UPS. Each checkpoint is ZIP/CRC checked and SHA256 recorded. Retention is the
24 most recent checkpoints plus one per day for up to 14 days. Factorio also has
five rotating autosaves. Saves block briefly; experimental nonblocking saving is
disabled. The service makes a final checkpoint on a normal stop. The observed scheduled
checkpoint took about six seconds including ZIP verification; the exact in-game
pause still needs measurement during live play.
Administrative commands and scheduled saves can advance a tick while otherwise
paused; there is no continuous idle RCON polling.

- Live world and rotating saves: `/srv/factorio/saves/`.
- Verified history: `/srv/factorio/backups/`.
- Untouched original: `/srv/factorio/archive/pie-original.zip`.
- Initial off-machine copy: `outputs/netcup-access/backups/initial-checkpoint.zip`
  in this local task. This is a deployment recovery copy, **not ongoing offsite replication**.
- The matching mods remain on Netcup and locally under `work/pie-benchmark/mods`;
  preserve their exact versions when restoring.

For recovery, first stop `factorio` (which also stops adaptive control). Keep the
existing live saves by moving them to a dated recovery directory outside `saves/`.
Copy the chosen verified backup into the now-empty live save directory as
`world.zip`, set owner `factorio:factorio` and mode `600`, then start `factorio`.
It loads the newest live save and the controller starts at fallback. Verify the
loaded save and game tick in the journal before admitting players. Never leave an
unwanted newer autosave in the live directory: `--start-server-load-latest` would
select it. A fresh startup can take roughly 1–2 minutes for this mod pack.

Keep an off-machine backup of the world, exact mods, `/etc/factorio`, service files
`/var/lib/factorio-access` and `/opt/adaptive-ups` before upgrades. Configuration archives contain credentials
and need private storage. Pin Factorio and mod versions; do not auto-update game mods.

## Network and HTTPS

Public game: UDP 34197. Public website/helper: TCP 80/443. SSH: TCP 22.
RCON is bound to `127.0.0.1:27015`; acknowledgement receiver to `127.0.0.1:8765`.
The nftables firewall blocks other inbound ports and permits ICMP/DHCP replies.
Factorio runs as an unprivileged account; public listing is disabled, account
verification remains enabled; the initial setup whitelist has been removed.

Caddy 2.11.4 came from its signed official Debian repository. It obtains and
automatically renews a public Let's Encrypt certificate for **94.16.31.89** using
the `shortlived` profile. Keep ports 80/443 reachable for renewal. The provider
hostname's shared certificate quota prevented reliable issuance, so profiles use
the IP directly. Check `journalctl -u caddy` if HTTPS fails; do not disable TLS
validation. The game remains at fallback if helpers cannot reach the receiver.

Journals are limited to 300 MB and seven days. Raw Factorio startup logs contain
its command-line RCON password and stay private; use the redacted service journal
for troubleshooting. Static website downloads contain no profiles or credentials; `/join` privately returns each freshly generated helper file.

Pausing reduces idle computation. It does not change the provider's fixed rental
charge. No additional paid service was added for this deployment.

## Companion 0.3.0 and controller status

The companion caches its profile and chosen folder under the current user's
LocalAppData/FactorioAdaptiveUPS. It reuses older valid profiles. Advanced > Repair
access requests a replacement without invalidating older profiles. Reporting starts
automatically; the game is launched and joined manually by the player.

The controller learns each connection's lowest delay and never raises that baseline
to absorb backlog. GET /status returns only cached measurements, player report
health, controller reasons and conditional increase timing. It exposes no marker
session, credentials or RCON access, and performs no game query.
