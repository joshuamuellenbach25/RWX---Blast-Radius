# Status

Updated 2026-11-21 01:45 EST (practice board read at 01:30:55).

| Measure | Seconds |
|---|---|
| Our best practice score | 256.0 |
| Best score on the practice board | 142.9 |
| Our target (that score minus 10%) | 128.61 |
| Local run of 304103a on the 30 sample commits (scorer flags) | 148.3 |
| Main on the 30 sample commits, sample answers as the oracle (estimate) | 130.7 |

Open problem: the local test runs prune nothing on the practice fleet. The leading explanation is wall-clock test limits ending CPU-starved runs early; test limits now count CPU seconds.
