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

File	Command	Result	Note
chaos-07.txt	<command>	<result>	<what it was>
chaos-08.txt	run.py (mixed)	PASS	8 workers, 218 kills, 119 retried
chaos-09.txt	run.py --kill-mode random	PASS	8 workers, 223 kills, 115 retried
zombie-02.txt	zombie_test.py	PASS	second run, same result as zombie-01

File	Command	Result	Note
zombie-03.txt	zombie_test.py --duration-ms 9000 --freeze-s 25	PASS	baseline, clean code, overlap version
zombie-04.txt	same	PASS	clean code after a first mutate apply/restore; the mutation test was not run in between

File	Command	Result	Note
zombie-05.txt	zombie overlap, mutation no_attempt_check	FAIL (expected)	stale write won
zombie-06.txt	zombie overlap, mutation no_fencing_at_all	FAIL (expected)	2 effect rows
chaos-10.txt	run.py --jobs 1000 --no-kill, mutation plain_claim	FAIL (expected)	103 double claims
chaos-11.txt	run.py --jobs 1000 --no-kill, mutation ignore_cancel	FAIL (expected)	50 broken cancels
chaos-12.txt	run.py --jobs 500 --timeout 150, mutation no_reaper	FAIL (expected)	9 stuck jobs

File	Command	Result	Note
heartbeat-01.txt	heartbeat_test.py	PASS	baseline, clean code
heartbeat-02.txt	heartbeat_test.py, mutation no_heartbeat	FAIL (expected)	job re-claimed at 30.4s, cancel by reaper after 26.6s
heartbeat-03.txt	heartbeat_test.py, mutation no_cancel_in_heartbeat	FAIL (expected)	cancel took 56.9s