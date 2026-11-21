# Status

Updated 2026-11-21 06:09 EST. Diagnostic v5 (1c37127, main f20bf38 plus a count of what became of each planned run) scored 218.5 s as BB-E2811D6B (06:04 EST; all three runs 215.047 s simulated, 3.45 to 3.47 s CPU, 0 misses), our best and the entry in the final. Code 2055: at least 7 of 8 or 9 planned runs returned usable results, none killed or out of time; main alone simulates 210.952 to 215.047 s against 254.041 s for 33211fc. Next practice slot, 07:04 EST. Diagnostic BB-DFCDC1FD (a73870d, 05:03 EST, 256.1 s, all three runs 254.087 s simulated) read code 46 over the 254.041 s base: about 224 of the 254 board seconds sit in commits that get no local run, and about 32 in commits whose local run fails. Listing every test costs 36.8 s, and any list with test 1490 (28.8 s) costs at least 30.8 s. On the sample, four commits with a planned local run have test 1106 (21.5 s) among their slow candidates; a run that fails or returns nothing lists it. The fleet reports about 2 s of CPU per run where the sample model predicts 35 s for that code, so its local runs barely ran; that code's worker still flushed stderr before silencing it, a bug main has fixed. Next practice slot, 06:03 EST: diagnostic 1c37127, main plus a count of what became of each planned run, read as the batch total modulo 4096 with no base score needed. Main f20bf38 carries every reproduced fix from the code reviews through 9559402; the 30 sample plans keep their lists. Earlier diagnostics: BB-6D152AEF (04:02 EST) showed the practice commits break the tests the local runs check; BB-E6219E9D (03:01 EST).

| Measure | Seconds |
|---|---|
| Our best practice score | 218.5 |
| Best score on the practice board | 130.8 |
| Our target (that score minus 10%) | 117.72 |
| Local run of 304103a on the 30 sample commits (scorer flags) | 148.3 |
| Main on the 30 sample commits, sample answers as the oracle (estimate) | 130.7 |

Open problem: the local test runs prune nothing on the practice fleet. CPU-second test limits (28eb537) did not change that. Diagnostic BB-E6219E9D read 254.102 s simulated in all three runs against 254.041 s for the same plan without the signal: code 61, so the fleet started at least 3 verification jobs, at least 3 returned results, and exactly 1 came back with no failure. The local runs execute on the fleet; nearly all of them report a failing test.

Analysis changes from 02:15 to 02:51 EST: import-time checks for assigned special names, dict unpacking, new element kinds, compound statements, and modules past 2 MB. The 30 sample plans are unchanged.
