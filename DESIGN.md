# Design Note: Training-Job Queue

Stack: Python, FastAPI, Postgres, N worker processes, Docker Compose.
Status: **updated after implementation and chaos testing**. The design describes the final queue behavior and records important test findings below.

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

## 9. Known weaknesses and tested limits

- DB is a single point of failure. The chaos tests killed workers, not Postgres, so database-failure recovery is not covered.
- Polling for jobs wastes queries while the queue is idle. `LISTEN/NOTIFY` could reduce idle polling, but the current implementation keeps polling for simplicity.
- The 30-second lease is a recovery-versus-false-requeue tradeoff. In the starvation and freeze tests, recovery occurred at about 29.9 seconds and the frozen job was reclaimed at about 31.6 seconds.
- `ORDER BY created_at` with `SKIP LOCKED` was not shown to cause a correctness failure in the tested eight-worker runs. Higher-scale contention remains a performance limit to investigate.
- A job that outlives its lease without a working heartbeat can execute more than once, but fencing and the unique effect constraint prevent duplicate effects. The heartbeat and freeze tests confirmed this behavior.

## 10. What changed after testing

- 2026-11-10: the smoke test and idempotent-create check passed.
- Worker crashes initially exposed two test-harness issues: cancellation could hide retries, and worker containers did not restart automatically after `docker kill`; the runs explicitly started killed workers again.
- Zombie, heartbeat, freeze, and mutation tests confirmed the attempt fence, heartbeat lease checks, `SKIP LOCKED`, cancellation handling, reaper behavior, and unique effect constraint.
- `starvation-01` exposed a real reaper starvation bug: reaping only between jobs left an expired job running for about 79.7 seconds while all workers were busy. The reaper was moved to a background thread; `starvation-02` recovered the orphan at about 29.9 seconds.
- A failing job cancelled while running exposed a second real bug: the failure completion path ignored `cancel_requested`. The failure path was changed to use the same cancellation-aware completion rule as successful jobs.
- An isolation-level mutation produced HTTP 500 responses under repeatable read. Connections now explicitly use `READ COMMITTED`, and the final run reported no HTTP errors.
- Final chaos and load runs preserved the intended guarantee: no lost jobs or duplicate effects in the recorded scenarios. External side effects and database-failure recovery remain outside the tested scope.