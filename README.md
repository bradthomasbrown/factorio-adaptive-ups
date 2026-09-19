# Adaptive UPS

An experimental Factorio server controller and player companion that adjust the
shared simulation speed when clients fall behind. Tested with Factorio **2.0.73**.
MIT licensed; Windows companion **0.3.0**, game mod **0.2.1**.

Pyandon server: **94.16.31.89:34197**. [Player setup](https://94.16.31.89/).
The distributed companion is configured for this server.

## Players

Download and open the companion from the setup page. It detects the Factorio
account name, obtains its own reporting credential, and waits for the game.
Choose the correct data folder if several installations are found. Start Factorio
and join manually; the companion has a Copy address button and never launches it.
Keep the companion open while playing. Existing helper profiles remain usable
through Advanced; ordinary setup needs no JSON download or import.

Until the mod is published on the Mod Portal, install the bundled mod once with
Factorio closed. Repeated installation recognizes identical files without changing
them. Factorio handles the rest of the server mod sync.

The Windows executable is currently unsigned. You can inspect/run the Python
source or build it yourself; the source includes no Factorio sign-in credentials.

## How it works

Each Factorio client simulates the full factory. When a client processes fewer
simulation steps per second than the server, pending steps accumulate. The mod
writes `script-output/adaptive-ups/beacon.json` when the local simulation processes
a checkpoint. The companion acknowledges that marker over HTTPS. The controller
measures elapsed time on its own monotonic clock and changes `game.speed` through
private RCON. This estimates checkpoint delay, not the exact debug buffer count;
network transit and file polling are included.

The minimum observed connection delay is learned separately from added delay.
It never increases to absorb a growing backlog. Recent medians, an increasing-delay
check and hard delay limits inform reductions. Once all connected players report
steadily, the controller tries higher speeds in small steps. A missing companion
holds the group at fallback. Clients slower than that fallback still need a lower
minimum; this does not make simulation faster or split it across machines.

The companion sends a name, marker session and sequence number with its own
reporting credential. It extracts the name from `player-data.json`; it never sends
the Factorio sign-in token. Mods cannot make ordinary HTTPS requests. Optional Lua
localhost UDP would still require an external program and a launch flag; files
avoid that requirement. See [Mod Portal description](MOD-PORTAL.md).

## Run, test and build

Python 3.10+ supports the source (Python 3.13 used for the Windows build). Tkinter
is needed for the GUI. No third-party runtime packages are required.

```powershell
python -m unittest discover -s tests -v
python build.py
python player_helper.py
```

To build the Windows x64 executable using Python 3.13 on Windows:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install PyInstaller==6.22.3
.venv\Scripts\python.exe build.py --exe
```

The executable appears in `dist/AdaptiveUPS.exe`. No PowerShell script execution
policy change is needed. `build.py` creates a deterministic ZIP from the mod
source. PyInstaller executables are not claimed to be byte-for-byte reproducible
across different machines. Linux/macOS source use is possible but not acceptance-tested.

## Server operation and limits

Components: `adaptive_ups.py` (controller), `player_helper.py` (GUI), `mod/`
(Factorio mod), `enrollment.py` and `access_store.py` (open enrollment with token
hashes), `server_launcher.py` (redacted logs), and `server_admin.py` (operations).
[Operator runbook](OPERATIONS.md) describes the current deployment.

The server's configured range is 15–25 UPS. Empty games pause. Join/leave events
write a roster file so the controller does not continuously wake a paused game.
The public `/status` endpoint serves a cached, sanitized controller snapshot; it
never queries the game. The GUI distinguishes measured UPS from requested UPS,
shows delayed/missing player reports, and qualifies increase countdowns.

Keep RCON on loopback and put TLS in front of reporting/enrollment. Helper profiles
are not game administrator credentials. The setup page is an open invitation;
Factorio verifies game accounts. Enrollment does not prove ownership of a name.
This is a cooperative community aid, not an anti-cheat system.

Controller ownership, watchdog fallback, network transport, credential reuse,
repeat installation, and controller delay behavior have automated checks.
Long-duration multiplayer acceptance is separate and remains pending for 0.3.0.
Capacity benchmark results will be recorded separately; a requested speed is not
proof that the hardware sustains it.
