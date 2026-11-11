### Run 01: 200 jobs, kills on (RUNS/chaos-01.txt)
- Result: PASS, 12/12 invariants. But the harness warned that no job was retried, so the run proves nothing about crash recovery.
- What happened: 8 kills, 2 of them hit workers holding a job. Those 2 jobs stayed "running" for ~30s (the lease), then ended "cancelled", not retried.
- Cause: my canceller kept picking currently-running jobs, including these orphans, so they carried cancel_requested when the reaper found them. The reaper then finalised them as cancelled.
- This is a flaw in the harness, not in the queue. One real finding: a job whose worker dies after a cancel request ends "cancelled" and does not get stuck.

### Run 02: 1000 jobs, no kills (RUNS/chaos-02.txt)
- Control run. PASS, 12/12. 0 retries, as expected. ~25 jobs/s throughput with 8 workers (simulated work 50-500ms).
- Its worker-log counts (reaper actions=2) are wrong: they include run 01's log lines. Fixed in the harness afterwards.

### Run 03: 10,000 jobs, kills on (RUNS/chaos-03.txt)
- Result: PASS, 12/12 invariants. 72 kills, but only 7 jobs were claimed more than once (attempt=2). ZOMBIE rejections = 0.
- Interpretation: the 7 retries are real evidence of crash recovery (killed mid-job, re-run, exactly one effect). But 72 kills with 7 retries means most killed jobs were cancelled by the cancel traffic instead (same cause as run 01).
- The zombie/fencing code never ran in this run, so it is untested by it.

### Harness problems found (and fixed after run 03)
1. Cancels swallowed crashes (above). Fix: the canceller no longer targets jobs that have been running > 1.5s, except 10% of picks.
2. Kills came every ~4.7s, not ~2s (the restart check blocked the loop). Fix: scheduled kills, non-blocking restart check.
3. Worker-log counts included earlier runs. Fix: logs filtered with --since taken from the DB clock.
4. The reaper count was labelled "requeued" but also counts cancel-finishes and failures. Fixed label.
5. Added a mode that kills only workers that are holding a job (--kill-mode busy / mixed).

### Docker restart policy
- In all 72 + 8 kills, the harness had to run `docker start`: `restart: always` did not restart a container killed with `docker kill`.
- Manual check: <paste what you see after Part 2 step 2>. Cause: <your finding, or "unknown">.
- Consequence: automatic restart after a real crash is not verified by these runs.

### Not yet changed in the design
- No bug has been found in the queue itself so far. I do not trust that yet: the retry and zombie paths have barely been exercised (see runs after the harness fix below).

### Zombie test 01 (RUNS/zombie-01.txt): PASS
Worker froze 40s past a 30s lease. Job was re-claimed as attempt 2 and succeeded. The frozen worker woke up 10s later and its write for attempt 1 was rejected ("ZOMBIE write rejected"). Exactly one effect row, from attempt 2.

### Runs 04-06: 7 workers, not 8
Worker-1 was left stopped after a manual `docker kill` experiment. The harness warned but continued. These runs are kept but must not be counted as 8-worker runs. Harness now aborts on a wrong worker count.

### Run 04: 0 retries even after the cancel fix
After the queue drained, the only running jobs were orphans, and the canceller kept targeting them; the reaper finished them as cancelled. My fix (cancel only 10% of stale jobs) was not enough for short runs.

### Runs 05, 06: retry path exercised
154 and 167 jobs were claimed 2-3 times after worker kills; every invariant held, exactly one effect per succeeded job.

### Not covered yet
- Heartbeat/lease extension: no chaos job ran longer than the 10s heartbeat interval.
- Zombie under load: kill -9 produces no zombies; need a freeze (docker pause) actor.
- Docker restart policy does not restart containers after `docker kill` (confirmed by hand: Exited (137)); cause unknown.

### Runs 08 and 09: first valid 8-worker runs
- 10,000 jobs, 218 and 223 kills at an average gap of 2.0s, 119 and 115 jobs retried, all 12 invariants held.
- ~450 cancels per run were accepted while a job was running; all ended "cancelled".
- Steady state shows ~14 jobs in "running" with a dead worker (kill every 2s x 30s lease). Recovery latency for a crashed worker's job is the lease length.
- Throughput ~22 jobs/s with kills (25/s without, run 02). Creates accepted at ~130/s. Latency not measured.
- No bug found so far. I do not treat that as proof: the harness has not yet been shown to fail (mutation test planned), and the heartbeat and zombie paths did not run under load.

### Zombie test 02: PASS (same as 01)


### Overlap zombie test on clean code (zombie-03, zombie-04): PASS
Worker froze for 25s after a 9s job. Attempt 2 was claimed at ~31s and was still running when the old worker woke at ~35s. The stale write was rejected ("ZOMBIE write rejected attempt=1") about 5s before attempt 2 finished. At that moment the job status was 'running', so only the attempt check could reject it. One effect row, from attempt 2.


### Mutation testing (5 deliberate breakages, all detected; files zombie-05/06, chaos-10/11/12)
- Removing the attempt check: the stale write won silently (effect from attempt 1, job row still says "succeeded, attempt 2"). Only the effect.attempt = jobs.attempt check caught it.
- Removing attempt + status checks + UNIQUE: 2 effect rows for one job.
- Claim without SKIP LOCKED: 103 of 1000 jobs claimed twice, yet all original invariants passed because fencing discarded the loser's write. Only a new no-kill check caught it. So SKIP LOCKED prevents duplicated work; fencing keeps the result correct. For an external side effect this would be a real duplicate execution.
- Finish ignoring cancel_requested: all 50 accepted cancels ended "succeeded".
- Reaper disabled: 9 jobs stayed "running" for 150s (lease is 30s); cancels accepted on them were never completed.
- Harness gap found: in kill runs a double claim looks like an ordinary retry. Fix: ZOMBIE rejections in runs without frozen workers now fail the run (no_fenced_writes). Runs 08 and 09 had 0 rejections, so no double claims occurred in them.
- Limits: mutation 2 removed three protections at once; heartbeat, idempotent create, isolation level and the cancel statement are not mutation-tested yet.


### Heartbeat test (heartbeat-01..03)
- Clean code: a 45s job survived a 30s lease (attempt stayed 1, lease_until moved forward 40s, one effect). A running job cancelled 3.8s after start was cancelled 6.4s later; the worker logged "cancel observed".
- No heartbeat: the job was re-claimed at 30.4s while its worker was alive; the cancel was only completed by the reaper after the lease expired (26.6s).
- Heartbeat without cancel check: the lease was still extended, but a cancelled 60s job ran to the end (56.9s). Result correct (cancelled, no effect), work wasted.
- Design consequence: cancel of a running job takes up to one heartbeat interval (10s). Heartbeat writes scale with running jobs / 10s.
- Not covered: the "lost lease, abandon" branch has never executed; the heartbeat runs in the same loop as the work, so a real blocking job would need a heartbeat thread, which brings the opposite problem (hung job with a live heartbeat holds its lease forever).

### Clock skew mutation (chaos-15)
- Mutation: the worker computed lease_until from its own clock, set 40s behind (lease 30s), so every lease was born expired.
- Result: 199 of 300 jobs were claimed more than once (attempt up to 5), 437 stale writes were rejected by the attempt check, 9 jobs ended 'failed' after 5 attempts although every worker was healthy and every run of the work finished.
- No double effect: fencing and UNIQUE(job_id) held. But the work itself ran many times (437 wasted completions) and 9 jobs were lost to the retry limit. With a real external side effect these would be duplicate executions.
- Takeaway: clock skew is silent. Safety of the effect survives, liveness and cost do not. Our rule (all lease times come from the DB now(), never from a worker) is what prevents this; this mutation violates it on purpose.
- Limits: one run, constant 40s offset, only the claim path mutated (heartbeat still uses DB time), 300 jobs, no kills.


ИИ предсказал двойные захваты при clock_behind. Первый прогон (chaos-14) прошёл без единого retry; я не принял это как опровержение, проверил наличие метки мутации и кода в контейнере, и повторил (chaos-15)

### Isolation level mutation (chaos-16)
- Mutation: database default isolation changed from READ COMMITTED to REPEATABLE READ (API and workers).
- Result: all 13 invariants passed, yet the API returned HTTP 500 for 144 creates and 2 cancels, with "could not serialize access due to concurrent update" in api and worker logs. 2 worker loop errors. One job stayed 'running' about 20s after the queue emptied and was finished by the reaper as cancelled (interpretation, to be confirmed from the worker log).
- No double effect and no lost job: fencing and UNIQUE constraints still hold. Creates were saved by client retries with the same key; the 2 failed cancels were not retried.
- Takeaway: correctness of effects does not depend on the isolation level, availability does. The code has no retry on serialization failure and relies on the database default.
- Harness gap: HTTP 500s are only printed, not counted as a failed invariant, so this run says PASS. Found by reading the stats, not the PASS line.
- Limits: one run, no kills, only the global default changed (not SERIALIZABLE), cause of create 500s inferred from Postgres documentation.

### Reaper starvation (starvation-01, found on clean code; fixed; starvation-02)
- The reaper ran only in the worker's main loop, between jobs. With every worker busy on an 80s job and one killed (not restarted), the dead worker's job stayed 'running' for 79.7s although its lease expired at 30s. Nobody was free to reap.
- Chaos runs with jobs under 0.5s hid this completely.
- Fix: the reaper runs in a background thread in every worker process. Re-test: orphan recovered at 29.9s. Chaos run 17 (2000 jobs, 60 kills): 12/12 invariants. Mutation no_reaper still fails the tests (chaos-18), so the tests did not become vacuous.
- Still open: a frozen process freezes its reaper thread too; if all worker processes die nobody reaps; 8 reapers poll every 2s.

### Freeze during work (freeze-01)
- A 50s job; its worker's container was paused (docker pause) 3s after start for 40s (lease 30s). Another worker's reaper requeued it, attempt 2 started at 31.6s. After unpause the old worker immediately hit its overdue heartbeat, found the lease gone ("lost lease ... abandoning") and stopped. One effect, from attempt 2.
- First run of the lost-lease branch. Limits: one run, one freeze point; pause during finish, between heartbeats and of the reaper thread itself not tested; log-line-to-container mapping inferred from timing.

### Freeze test mutation (freeze-02)
- Without the attempt check in the heartbeat the frozen worker never noticed it had lost the job; its write was rejected only at finish ("ZOMBIE write rejected"). Final data still correct: one effect, from attempt 2.
- Takeaway: the heartbeat fence saves wasted work; correctness rests on the fence at finish plus UNIQUE(job_id).
- Not shown: whether such a worker could extend the new attempt's lease (it did not finish its work in this run, timing inferred).

### Load (load-01, load-02) and pagination (pagination-01)
- 100 req/s mixed (50% create, 30% get, 10% list, 10% cancel), 60s, open-loop client, 50ms jobs: 0 errors, p99 226ms, queue never above 5, 0 double effects. Client lag ~1ms, so numbers describe the server.
- Limits: one run, one machine, jobs of 50ms. Job throughput = workers / job duration, so with real long jobs the queue grows regardless of API speed; this test says nothing about that. Only 4 cancels hit running jobs. p99/p95 tail not investigated. p99 threshold (500ms) is arbitrary.
- Pagination: no duplicates or gaps, also during concurrent inserts. Late-committing rows with an older created_at are untested. Test not yet shown able to fail.

### Cancel vs fail (failcancel-02, found by reading code, confirmed on clean code)
- A job that was going to fail and was cancelled while running (HTTP 202) ended 'failed': FINISH_FAIL_SQL ignored cancel_requested. No effect row, so data stayed correct, but the API's promise was broken. The chaos runs never used the 'fail' payload, so they could not see it.
- Fix: FINISH_FAIL_SQL now uses the same CASE as FINISH_OK_SQL. Re-test: see failcancel-03.
- Limits: one scenario, one run; a cancel arriving between heartbeats of a failing job is not tested separately.

### Explicit isolation (chaos-20)
- The app now sets READ COMMITTED on every connection instead of relying on the DB default. Repeating the isolation_rr mutation: 0 HTTP 500 and 14/14 invariants (chaos-16 before the fix: 144 create + 2 cancel 500s).
- Added no_http_errors to the harness so availability problems fail a run.
- Limits: one 1000-job run, no kills; no_http_errors not yet shown to fail; the DB-level setting was not re-read with SHOW before this run.