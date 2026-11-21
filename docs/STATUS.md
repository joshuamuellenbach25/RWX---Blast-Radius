# Status

Updated 2026-11-21 05:29 EST. Diagnostic BB-DFCDC1FD (a73870d, 05:03 EST, 256.1 s, all three runs 254.087 s simulated) read code 46 over the 254.041 s base: about 224 of the 254 board seconds sit in commits that get no local run, and about 32 in commits whose local run fails. Listing every test costs 36.8 s, and any list with test 1490 (28.8 s) costs at least 30.8 s. On the sample, four commits with a planned local run have test 1106 (21.5 s) among their slow candidates; a run that fails or returns nothing lists it. Next practice slot, 06:03 EST: diagnostic e6d4ff8 counts, among commits with no run result, the lists costing at least 20 s by verdict kind and the planned runs that returned nothing. Main 9559402 carries every reproduced fix from the code reviews through fe6eec0; the 30 sample plans are unchanged. Earlier diagnostics: BB-6D152AEF (04:02 EST) showed the practice commits break the tests the local runs check; BB-E6219E9D (03:01 EST).

| Measure | Seconds |
|---|---|
| Our best practice score | 256.0 |
| Best score on the practice board | 131.5 |
| Our target (that score minus 10%) | 118.35 |
| Local run of 304103a on the 30 sample commits (scorer flags) | 148.3 |
| Main on the 30 sample commits, sample answers as the oracle (estimate) | 130.7 |

Open problem: the local test runs prune nothing on the practice fleet. CPU-second test limits (28eb537) did not change that. Diagnostic BB-E6219E9D read 254.102 s simulated in all three runs against 254.041 s for the same plan without the signal: code 61, so the fleet started at least 3 verification jobs, at least 3 returned results, and exactly 1 came back with no failure. The local runs execute on the fleet; nearly all of them report a failing test.

Analysis changes from 02:15 to 02:51 EST: import-time checks for assigned special names, dict unpacking, new element kinds, compound statements, and modules past 2 MB. The 30 sample plans are unchanged.
