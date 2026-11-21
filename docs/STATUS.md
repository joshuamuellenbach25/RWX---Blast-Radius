# Status

Updated 2026-11-21 03:37 EST (practice board read between 03:06 and 03:09). Diagnostic 8c1a217 submitted at 03:01 EST as BB-E6219E9D.

| Measure | Seconds |
|---|---|
| Our best practice score | 256.0 |
| Best score on the practice board | 138.1 |
| Our target (that score minus 10%) | 124.29 |
| Local run of 304103a on the 30 sample commits (scorer flags) | 148.3 |
| Main on the 30 sample commits, sample answers as the oracle (estimate) | 130.7 |

Open problem: the local test runs prune nothing on the practice fleet. CPU-second test limits (28eb537) did not change that. Diagnostic BB-E6219E9D read 254.102 s simulated in all three runs against 254.041 s for the same plan without the signal: code 61, so the fleet started at least 3 verification jobs, at least 3 returned results, and exactly 1 came back with no failure. The local runs execute on the fleet; nearly all of them report a failing test.

Analysis changes from 02:15 to 02:51 EST: import-time checks for assigned special names, dict unpacking, new element kinds, compound statements, and modules past 2 MB. The 30 sample plans are unchanged.
