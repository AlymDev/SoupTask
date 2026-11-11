# Index of runs

Raw run files in `RUNS/` are never edited or deleted, failed runs included. Every `chaos-NN.txt` has a `chaos-NN-worker-logs.txt` next to it. The notes in this index were corrected where they turned out to be wrong (see "Corrections" at the end).

How to read the run files:

- The `git commit` line in a run header is the commit at the time of the run. Uncommitted changes (an active mutation, a fix not yet committed) are not reflected in it. Use the `MUTATION ACTIVE` line and the notes below.
- The number of invariants changed over time. Runs with kills had 12 until `no_http_errors` was added (13). Runs with `--no-kill` have one more (`no_double_claim_without_kills`): 13, or 14 with `no_http_errors`. "12/12", "13/13" and "14/14" are therefore not directly comparable. [[TODO-USER: confirm these counts against the run files]]

| File | Command | Result | Note |
|---|---|---|---|
| 00-smoke-test.txt | 1 job by hand | OK | happy path only |
| chaos-01.txt | run.py --jobs 200 | PASS | vacuous: 0 retries (cancels hid crashes) |
| chaos-02.txt | run.py --jobs 1000 --no-kill | PASS | control run |
| chaos-03.txt | run.py (10,000) | PASS | 72 kills, 7 retries |
| zombie-01.txt | zombie_test.py | PASS | one stale write rejected, 1 effect (attempt 2) |
| chaos-04.txt | run.py --jobs 500 | PASS | 7 workers, 0 retries (orphans cancelled) |
| chaos-05.txt | run.py (mixed kills) | PASS | 7 workers, 154 retried, 266 kills |
| chaos-06.txt | run.py --kill-mode random | PASS | 7 workers, 167 retried, 280 kills |
| chaos-07.txt | [[TODO-USER: command]] | [[TODO-USER: result]] | [[TODO-USER: what it was]] |
| chaos-08.txt | run.py (mixed) | PASS | 8 workers, 218 kills, 119 retried |
| chaos-09.txt | run.py --kill-mode random | PASS | 8 workers, 223 kills, 115 retried |
| zombie-02.txt | zombie_test.py | PASS | second run, same result as zombie-01 |
| zombie-03.txt | zombie_test.py --duration-ms 9000 --freeze-s 25 | PASS | baseline, clean code, overlap version |
| zombie-04.txt | same | PASS | clean code after a first mutate apply/restore; the mutation test was not run in between |
| zombie-05.txt | zombie overlap, mutation no_attempt_check | FAIL (expected) | stale write won |
| zombie-06.txt | zombie overlap, mutation no_fencing_at_all | FAIL (expected) | 2 effect rows |
| chaos-10.txt | run.py --jobs 1000 --no-kill, mutation plain_claim | FAIL (expected) | 103 double claims |
| chaos-11.txt | run.py --jobs 1000 --no-kill, mutation ignore_cancel | FAIL (expected) | 50 broken cancels |
| chaos-12.txt | run.py --jobs 500 --timeout 150, mutation no_reaper | FAIL (expected) | 9 stuck jobs |
| heartbeat-01.txt | heartbeat_test.py | PASS | baseline, clean code |
| heartbeat-02.txt | heartbeat_test.py, mutation no_heartbeat | FAIL (expected) | job re-claimed at 30.4s, cancel by reaper after 26.6s |
| heartbeat-03.txt | heartbeat_test.py, mutation no_cancel_in_heartbeat | FAIL (expected) | cancel took 56.9s |
| chaos-13.txt | run.py --jobs 1000 --no-kill | [[TODO-USER: RESULT line from the file]] | no mutation marker in header: control run, clean code |
| chaos-14.txt | run.py --jobs 1000 --no-kill, run after `apply clock_behind` | PASS (13/13), 0 retries, 0 reaper actions | no MUTATION ACTIVE line in the header, so not evidence about clock_behind. [[TODO-USER: what did you find in the containers? Was it a control run on clean code?]] Repeated as chaos-15 |
| chaos-15.txt | run.py --jobs 300 --no-kill, mutation clock_behind | FAIL (expected) | 199/300 claimed >1x, 437 stale writes rejected, 9 'failed' with healthy workers |
| chaos-16.txt | run.py --jobs 1000 --no-kill, mutation isolation_rr | PASS (13/13) but 144 create + 2 cancel HTTP 500 | invariants hold, availability does not; the harness did not count HTTP errors as failures (fixed later, see chaos-20) |
| starvation-01.txt | starvation_test.py, clean code (before fix) | FAIL (real bug) | dead worker's job stayed 'running' 79.7s (lease 30s) while the other 7 workers were busy: reaper ran only between jobs |
| starvation-02.txt | starvation_test.py, reaper in background thread | PASS | orphan recovered at 29.9s (lease 30s) |
| chaos-17.txt | run.py --jobs 2000, after reaper fix | PASS (12/12) | 60 kills, 15 retried, 0 double effects |
| chaos-18.txt | run.py --jobs 500 --timeout 150, mutation no_reaper (after fix) | FAIL (expected) | 10 jobs stuck 'running', 25 accepted cancels never completed |
| freeze-01.txt | freeze_test.py, clean code (reaper thread) | PASS | worker paused 40s mid-work; attempt 2 claimed at 31.6s; old worker logged 'lost lease, abandoning' right after unpause; one effect from attempt 2 |
| freeze-02.txt | freeze_test.py, mutation heartbeat_no_attempt | FAIL (expected) | no 'lost lease'; stale write rejected only at finish (ZOMBIE); still one effect from attempt 2 |
| load-01.txt | load_test.py --duration-s 20, clean code | PASS | trial, 100 req/s mixed |
| load-02.txt | load_test.py, clean code | PASS | 6000 req at 100/s, p50 19ms p95 37ms p99 226ms, 0 errors, queue peak 5; only 4 cancels hit a live job (jobs last 50ms) |
| pagination-01.txt | pagination_test.py | PASS | 500 jobs, 14 pages, no dup/gap; inserts during walk: no dup, no old job lost |
| failcancel-02.txt | fail_cancel_test.py, clean code | final status 'failed' | real bug: job with payload fail=true, cancel accepted (HTTP 202) while running, ended 'failed' instead of 'cancelled'; FINISH_FAIL_SQL ignored cancel_requested. The RESULT line is unreliable: the test had a bug (cancel code overwritten) and could never print PASS |
| failcancel-03.txt | same, after a "fix" | final status 'failed' | the edit to worker.py had not been applied (image digest identical to the previous build): not a result for the fix; same test bug |
| failcancel-04.txt | same, fix applied (verified by grep in the container) | final status 'cancelled', no effect | RESULT line says FAIL because of the test bug, not the code |
| failcancel-05.txt | fixed test | [[TODO-USER: result from the file]] | [[TODO-USER: note]] |
| failcancel-06.txt | fail_cancel_test.py (fixed test) | PASS | cancel accepted (HTTP 202), final status 'cancelled', no effect rows. The fixed test has not been shown able to fail. [[TODO-USER: this row comes from your earlier notes; confirm against the file]] |
| chaos-19.txt | run.py --jobs 2000 | PASS (12/12) | ran on code WITHOUT the fail/cancel fix |
| chaos-20.txt | run.py --jobs 1000 --no-kill, mutation isolation_rr, after explicit READ COMMITTED | PASS (14/14) | 0 HTTP 500 (chaos-16 without the fix: 144 create + 2 cancel 500s). Prior SHOW not rerun |
| chaos-21.txt | run.py (10,000 jobs) on the final code (commit 1b91f55: reaper fix, FINISH_FAIL_SQL fix, explicit READ COMMITTED) | PASS (13/13) | 249 kills (127 on busy workers), average gap 2.0s; 129 jobs claimed more than once (127 twice, 2 three times), 213 reaper actions; 12,400 repeated POSTs, 0 HTTP errors; 0 double effects, 0 stuck, 0 lost; 429 accepted cancels on running jobs, all ended 'cancelled'; ZOMBIE rejections 0, lost lease 0 (no worker was frozen); about 8.4 minutes, about 19 jobs/s (capacity under continuous kills with 50-500ms jobs; for 100 req/s see load-02). [[TODO-USER: this row comes from your earlier notes; confirm every number against the file]] |

[[TODO-USER: if RUNS/failcancel-01.txt or any other file exists that is not listed above, add a row for it, including aborted or failed attempts]]

## Corrections to earlier notes

- failcancel-02 and failcancel-03: the first notes did not mention that the test itself had a bug. The RESULT lines of failcancel-02 to failcancel-04 are unreliable; the final status and the effect rows in those files are what show the bug and the fix.
- starvation-01: the first note did not say "(before fix)".
- chaos-14: two earlier notes disagreed. One said the mutation state was not confirmed and the run should not be used as evidence; the other said it was a control run on clean code. They are merged in the table above and wait for your confirmation.