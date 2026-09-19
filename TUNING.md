# Live controller tuning — 19 September 2026

The 25 UPS maximum and two-minute recovery hold were controller choices, not
Factorio requirements. Production now trials a **15–60 UPS target range** with
`healthy_seconds=1.5`, `increase_interval=0.5`, and `recovery_seconds=12`.
Previously these were 15, 5, and 120 seconds. No game, mod, or companion restart
is required for players; only the server controller was restarted.

The one-second controller loop limits the new ramp to about +2 requested UPS per
second. Calibration still requires eight reports spanning at least seven seconds;
the twelve-report window, minimum learned delay, reduction checks, immediate
critical fallback, roster handling, and mod watchdog remain in place. Shorter
waits cannot safely bypass delayed or missing reports.

## Observations

These are short, sequential, single-player live trials on the evolving production
save, not matched capacity benchmarks. The player moved around and used remote
view. Report delay includes simulation scheduling, network transit and file
polling; it is not a direct reading of the game's debug buffer.

| Trial | Duration | Median measured UPS | Report-delay median | Observation |
|---|---:|---:|---:|---|
| Focused, original controller | 45 s | 15.00 | 0.278 s | All reports classified as keeping up; recovery hold active |
| Background, original controller | 45 s | 15.00 | 0.307 s | Delay rose to roughly 0.7 s near the end; 19 delayed-report samples |
| Background, temporary Windows policy | 55 s | 15.00 | 0.305 s | All reports keeping up |
| Background, Windows policy restored | 45 s | 16.99 | 0.318 s | Reports stayed healthy; original controller ramped to 25 |
| Focused, faster waits, maximum 25 | 110 s | 15.01 | 0.784 s | Reached target 25 at +10 s, reduced to 15 at +44 s as reports became late |
| Focused, faster waits, maximum 60 | 180 s | 24.895 | 0.292 s | Reached target 60 at +28 s; measured peak 28.22 |
| Background, faster waits, maximum 60 | 120 s | 15.00 | 0.958 s | Already at fallback when recording began; 115 delayed-report samples |

Times above start with the recording, several seconds after controller restart.
During the 60-ceiling trial, the requested 60 persisted for about 140 seconds,
until the regular scheduled save at 20:57:57–20:58:02 UTC interrupted reporting.
The controller fell back and began increasing again within a few seconds. The
game remained connected. A snapshot showed Multiplayer UPS 25.0 and buffer
0/0/1 while target speed was 60; the top FPS/UPS line alone was not evidence of
60 world updates per second.

The short trials do not establish that a higher ceiling cures delayed reports,
or that the faster settings are stable for every player. The 25-ceiling trial
still spent much of its time at fallback. The prior [matched benchmark](CAPACITY.md)
remains the stronger comparison of the two machines' computation capacity.

## Background performance

The temporary Windows experiment explicitly disabled execution-speed throttling
and ignoring timer-resolution requests **for the running Factorio process only**.
Its original policy was saved and restored successfully. No CPU priority, global
power plan, registry setting, graphics setting, or companion behavior was changed.

The improved delay persisted after restoration, so this experiment does **not**
establish a Windows fix. Process policy masks describe explicit settings, not
every automatic scheduling decision. The machine was on AC power throughout.

Windows documents different performance treatment for focused and background
applications, plus timer-resolution changes for occluded windows. NVIDIA also
offers a Background Application Max Frame Rate setting. Those are candidates
to check, not established causes here. A driver's frame-rate limit and the
game's simulation rate are different quantities; measure both after any change.

In the final background sample, the client stayed connected but report delay
ranged from 0.539 to 1.887 seconds. A debug snapshot showed FPS/UPS 15/15,
Multiplayer UPS 15, latency 25 ticks and buffer 12/9/9. The background slowdown
is **unresolved**. The installed NVIDIA control panel did not open during the
read-only check, and NVAPI returned setting-not-found for the queried IDs; neither
result establishes whether a driver background limit is enabled. No driver
configuration changes were made.

Do not ignore a background player's live reports based on their previous best
speed. If that player really falls behind, remembering a higher capacity does
not execute their missing simulation steps. A profile could inform an initial
probe, but current delays must still override it.

## Validation and rollback

34 automated tests pass, including a simulated client capacity drop from 70 to
20 UPS with the faster profile and a 60 ceiling. The simplified tick-queue model
checks recovery but does not reproduce Factorio's rendering/network scheduler.
No extended multiplayer acceptance claim is made.

To restore the previous controller tuning, see [operations](OPERATIONS.md).
Private server backups preserve the former configuration and source. The mod and
Windows companion downloads are unchanged; bundled mod installation remains the
supported route and Mod Portal publication is deferred.

References: [Microsoft quality of service](https://learn.microsoft.com/en-us/windows/win32/procthread/quality-of-service),
[Microsoft process power controls](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessinformation),
[NVIDIA graphics settings](https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-us/mergedProjects/nv3d/Manage_3D_Settings_(reference).htm).
