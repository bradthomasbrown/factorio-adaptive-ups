# Matched capacity comparison — 19 September 2026

| Machine | Pass 1 | Pass 2 | Pass 3 | Combined UPS |
|---|---:|---:|---:|---:|
| Local Ryzen 7 4800H, Windows | 27.05 | 27.29 | 27.11 | **27.15** |
| Netcup EPYC 9645 VM, Linux | 24.22 | 20.02 | 21.92 | **21.92** |

Three fresh map loads on each machine, with 3,600 measured simulation updates per
pass. Combined UPS is 1,000 divided by the mean milliseconds per update, not the
average of reported UPS. Load times are excluded.

Both sides used Factorio 2.0.73, the original save SHA256
`faf3c6d23d5d26d73493eb99239177dcf8706fbfaa19fa7c1eae12e49a41d69e`, 45 identical mod
ZIPs including Adaptive UPS 0.2.0, identical mod settings and semantically identical
enabled-mod lists. Every pass ended with simulation checksum **1158198439**.
The first local attempt used a later saved state and was discarded.

The local game remained at its main menu; benchmarking used another isolated
process. Netcup's production game was paused while the isolated benchmark ran.
The remote benchmark peaked at 3.2 GB, below its 7 GB test limit. Production has
no CPU or memory quota configured. No swap/steal appeared in a short *post-test*
sample; this does not rule out host contention during the benchmark. Host load
and clock frequency were not continuously sampled, so the source of remote
variation remains unproven.

The rented machine delivered about **19% less simulation throughput** overall
than the laptop in this test. It can exceed 15 UPS, but this evidence does not
support a sustained 45 UPS expectation. Earlier local adaptive testing reached
a **requested** 44 UPS; actual measured speed was closer to 28–30 at the end.

Separately, the old controller fixed its connection-delay baseline at at most
0.6 seconds and required delay within another 0.2 seconds before increasing.
The observed remote client's typical report delay was about 1.0 second, leaving
it stuck at 15 despite stable reports. Controller 0.3.0 learns the lowest observed
delay, keeps it from drifting upward with backlog, and checks recent delay growth.
Automated checks pass; extended live multiplayer acceptance remains pending.

The current target range remains 15–25. Target UPS is a ceiling requested through
`game.speed`; measured UPS can be lower. This comparison is not a guarantee of
long-duration multiplayer performance, and future factory growth can change it.

Machine-readable evidence: [benchmark-comparison.json](benchmark-comparison.json).
