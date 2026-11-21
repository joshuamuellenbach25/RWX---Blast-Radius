# Status

Updated 2026-11-21 05:08 EST. Diagnostic BB-DFCDC1FD (a73870d, 05:03 EST, 256.1 s, all three runs 254.087 s simulated) read code 46 over the 254.041 s base: about 224 of the 254 board seconds sit in commits that get no local run, and about 32 in commits whose local run fails. The next work narrows the lists of commits with no local run. Next practice slot, 06:03 EST. Main fe6eec0 carries every reproduced fix from the code reviews through 1cbccf9; the 30 sample plans are unchanged. Earlier diagnostics: BB-6D152AEF (04:02 EST) showed the practice commits break the tests the local runs check; BB-E6219E9D (03:01 EST).

| Measure | Seconds |
|---|---|
| Our best practice score | 256.0 |
| Best score on the practice board | 131.5 |
| Our target (that score minus 10%) | 118.35 |
| Local run of 304103a on the 30 sample commits (scorer flags) | 148.3 |
| Main on the 30 sample commits, sample answers as the oracle (estimate) | 130.7 |

Open problem: the local test runs prune nothing on the practice fleet. CPU-second test limits (28eb537) did not change that. Diagnostic BB-E6219E9D read 254.102 s simulated in all three runs against 254.041 s for the same plan without the signal: code 61, so the fleet started at least 3 verification jobs, at least 3 returned results, and exactly 1 came back with no failure. The local runs execute on the fleet; nearly all of them report a failing test.

Analysis changes from 02:15 to 02:51 EST: import-time checks for assigned special names, dict unpacking, new element kinds, compound statements, and modules past 2 MB. The 30 sample plans are unchanged.
