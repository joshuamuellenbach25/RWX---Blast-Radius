# Status

Updated 2026-11-21 02:55 EST (practice board read at 02:31:04).

| Measure | Seconds |
|---|---|
| Our best practice score | 256.0 |
| Best score on the practice board | 138.1 |
| Our target (that score minus 10%) | 124.29 |
| Local run of 304103a on the 30 sample commits (scorer flags) | 148.3 |
| Main on the 30 sample commits, sample answers as the oracle (estimate) | 130.7 |

Open problem: the local test runs prune nothing on the practice fleet. CPU-second test limits (28eb537) did not change that. A diagnostic run at 03:00 EST reports how many verification jobs the fleet started, ran and finished clean.

Analysis changes from 02:15 to 02:51 EST: import-time checks for assigned special names, dict unpacking, new element kinds, compound statements, and modules past 2 MB. The 30 sample plans are unchanged.
