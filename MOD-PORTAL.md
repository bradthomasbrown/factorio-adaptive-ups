# Adaptive UPS

Adjust a multiplayer server's simulation speed so connected players can keep up.
Experimental; tested with Factorio 2.0.73.

**This mod requires an external server controller and a companion on each player's
computer. Installing the mod alone does not enable adaptive speed.**

Every client simulates the whole factory. If a client cannot process simulation
steps as quickly as the server, unprocessed steps accumulate and the client falls
behind. This mod writes a tiny local marker when that client reaches a checkpoint.
The companion reports the marker over HTTPS. The server controller measures delay
and adjusts `game.speed` for everyone. It starts conservatively, slows down when
reports become late, and gradually tries higher speeds when reports settle.

No world splitting, different player timelines, or extra simulation performance
is provided. A client slower than the configured minimum still needs a lower
server speed. Network problems and a stopped companion can also delay reports.

## Players

Use your server's setup instructions and companion configuration. Keep the
companion open while playing; start Factorio and connect normally yourself.
The companion does not launch the game or join for you.

The Pyandon community server's instructions are at https://94.16.31.89/.
That packaged companion is configured for this specific server.

## Server operators

Source, build instructions, Python controller and Windows companion:
https://github.com/bradthomasbrown/factorio-adaptive-ups

Keep RCON private. Controller ownership, fallback watchdog, authenticated marker
reports and rate-limited open enrollment are included. The operator sets the
minimum and maximum speed. Empty-server monitoring uses an event-written roster
file rather than continuously querying a paused game.

The reporting system estimates checkpoint delay, not Factorio's exact debug
buffer count. Its timing includes simulation backlog, network transit and file
polling. It is a cooperative performance aid, not an anti-cheat system.

## Why a separate program?

Mods cannot make ordinary HTTPS requests. Lua runs inside the deterministic
simulation, while the companion can report when each computer actually processes
a checkpoint. The mod writes `script-output/adaptive-ups/beacon.json`; the
companion sends only the player name, marker session and sequence, with its own
reporting credential. The controller uses its own monotonic clock. No computer
clock, research milestone or inventory is sent, and Factorio sign-in tokens are
not transmitted. Optional localhost Lua UDP would still require an external
program and a game launch flag; this file-based design needs no such flag.

MIT licensed. The Windows executable is currently unsigned; you can inspect and
run the Python source or build the executable yourself.
