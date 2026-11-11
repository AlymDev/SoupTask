# Index of runs (nothing here was edited or deleted)
| File | Command | Result | Note |
|---|---|---|---|
| 00-smoke-test.txt | 1 job by hand | OK | happy path only |
| chaos-01.txt | run.py --jobs 200 | PASS | vacuous: 0 retries (cancels hid crashes) |
| chaos-02.txt | run.py --jobs 1000 --no-kill | PASS | control run |
| chaos-03.txt | run.py (10,000) | PASS | 72 kills, 7 retries |

File	Command	Result	Note
zombie-01.txt	zombie_test.py	PASS	one stale write rejected, 1 effect (attempt 2)
chaos-04.txt	run.py --jobs 500	PASS	7 workers, 0 retries (orphans cancelled)
chaos-05.txt	run.py (mixed kills)	PASS	7 workers, 154 retried, 266 kills
chaos-06.txt	run.py --kill-mode random	PASS	7 workers, 167 retried, 280 kills