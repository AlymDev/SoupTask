# Design Note: Training-Job Queue

Stack: Python, FastAPI, Postgres, N worker processes, Docker Compose.
Status: **written before the code**. Sections marked `TODO(update)` must be revised after the chaos runs.

## 1. Storage choice: Postgres, not SQLite

- Workers are separate processes that claim jobs concurrently. Postgres gives row-level locking and `FOR UPDATE SKIP LOCKED`, so workers never block each other on the same row.
- SQLite has a single writer lock. 8 workers plus the API would serialize on it, and `SQLITE_BUSY` errors would add noise to the chaos test.
- Postgres has well-defined isolation levels and `now()` as one clock for everyone (see clock skew below).
- Cost: one more container, and the DB becomes a single point of failure (see weaknesses).

## 2. Schema (sketch)

```
jobs(
  id              uuid PK,
  idempotency_key text UNIQUE NOT NULL,
  payload         jsonb,
  payload_hash    text,                -- detect same key with different body
  status          text CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  attempt         int  NOT NULL DEFAULT 0,   -- fencing token
  worker_id       text,
  lease_until     timestamptz,
  cancel_requested boolean NOT NULL DEFAULT false,
  created_at, updated_at, finished_at  timestamptz DEFAULT now()
)
effects(
  id bigserial PK,
  job_id  uuid UNIQUE NOT NULL REFERENCES jobs(id),   -- at most one effect per job
  attempt int, worker_id text, created_at timestamptz DEFAULT now()
)
```

Index: `jobs(created_at) WHERE status='queued'` for claiming, `jobs(lease_until) WHERE status='running'` for the reaper.

## 3. How a worker claims a job

One atomic statement, default `READ COMMITTED`:

```sql
UPDATE jobs SET status='running', attempt=attempt+1, worker_id=$w,
                lease_until = now() + interval '30 seconds'
WHERE id = (SELECT id FROM jobs WHERE status='queued'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, attempt;
```

- `SKIP LOCKED` means two workers cannot claim the same row. A locked row is skipped, not waited on.
- `attempt` is incremented on every claim. The worker keeps `(id, attempt)` as its **fencing token**.
- All time comes from the DB (`now()`), never from the worker clock.

## 4. How a dead worker is detected

- **Lease + heartbeat.** While running, the worker extends the lease every ~10 s:
  `UPDATE jobs SET lease_until = now()+30s WHERE id=$id AND attempt=$n AND status='running' RETURNING cancel_requested`.
  Zero rows returned means the worker lost the job and must stop immediately without writing anything.
- **Reaper** (runs in every worker loop, or as a separate task): 
  `UPDATE jobs SET status='queued' WHERE status='running' AND lease_until < now()`.
  If `attempt >= MAX_ATTEMPTS`, the job goes to `failed` instead (avoids poison-pill loops).
  If `cancel_requested` is set, the job goes to `cancelled` instead of back to `queued`.
- `kill -9` stops the heartbeat, the lease expires, the reaper requeues the job, and another worker re-claims it with `attempt+1`.

## 5. Zombie workers (frozen, then resumed after lease expiry)

The old worker still holds `attempt = n`, but the job is now at `attempt = n+1` (or already finished). Every write from a worker is **fenced**:

```sql
UPDATE jobs SET status='succeeded', finished_at=now()
WHERE id=$id AND attempt=$n AND status='running';
```

If it affects 0 rows, the worker is a zombie: it discards its result and writes no effect. Heartbeat uses the same `attempt` check.

## 6. Cancel

- `queued`: conditional `UPDATE ... SET status='cancelled' WHERE id=$id AND status='queued'`. Done instantly.
- `running`: `UPDATE ... SET cancel_requested=true WHERE id=$id AND status='running'`. The worker sees the flag on its next heartbeat or before commit and stops. The reaper also honors the flag if the worker is dead.
- terminal states: cancel is a no-op returning the current state (idempotent), not an error that changes anything.
- **Cancel vs complete race**: both are conditional UPDATEs on the same row, so Postgres serializes them. Completion is:
  `SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'succeeded' END WHERE id=$id AND attempt=$n AND status='running'`.
  The effect row is inserted only if the result was `succeeded`, in the same transaction. Exactly one outcome wins and the other sees 0 rows.

## 7. Guarantee

**At-least-once execution, exactly-once effect.**

- Execution is at-least-once: a job killed mid-run is retried, so the "work" may start more than once. At-most-once would lose jobs on `kill -9`, which violates "no job is lost".
- The effect is exactly-once because the effect `INSERT` happens **in the same transaction** as the fenced status change, and `effects.job_id` is `UNIQUE`. Either both commit or neither does. A zombie's fenced UPDATE matches 0 rows, so its effect never lands.
- **Caveat (honest limit):** this only holds because the effect lives in the same database. For a real external side effect (e.g. a call to a GPU cluster), exactly-once is not achievable by this design alone. It would need an idempotency key passed to the external system.

## 8. Idempotent create

`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. If nothing is returned, `SELECT` the existing job and return it (HTTP 200 vs 201). If the key exists with a different `payload_hash`, return 409. This survives client retries and concurrent duplicate POSTs because the uniqueness is enforced by the DB, not by application code.

## 9. Known weaknesses (to be tested)

- DB is a single point of failure. The chaos test kills workers, not Postgres. `TODO(update)`
- Polling for jobs wastes queries at idle. `LISTEN/NOTIFY` could help. `TODO(update)`
- Lease length (30 s) is a tradeoff: a longer lease means slower recovery, a shorter one means more false requeues under GC pauses or load. `TODO(update)` with measured recovery time.
- `ORDER BY created_at` with `SKIP LOCKED` can cause contention on the hot head of the queue at high worker counts. `TODO(update)`
- A job that is slower than its lease, with a stuck heartbeat, gets duplicated execution (but not duplicated effect). `TODO(update)`

## 10. What changed after testing

`TODO(update)`: fill this in after each chaos run. Include bugs the harness found and how they were fixed.
- 2026-11-10: smoke test passed (1 job). No design change. Failure behavior still untested.

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