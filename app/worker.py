"""Worker: claim -> run (with heartbeat) -> fenced finish. Also runs the reaper.

Test hooks in job payload (used by the silent-failure checks):
  duration_ms : how long the simulated work takes
  freeze_s    : (first attempt only, see freeze_attempts) sleep AFTER the work, before committing, with NO heartbeat (simulates a
                frozen process / long GC pause => becomes a zombie once the lease expires)
  fail        : true => the job ends in 'failed'
"""
import logging
import os
import random
import socket
import sys
import time
import uuid
import threading

from .db import (HEARTBEAT_SECONDS, LEASE_SECONDS, MAX_ATTEMPTS, REAP_INTERVAL_SECONDS,
                 WORK_MAX_MS, WORK_MIN_MS, make_pool)

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

# Unique per process start, so a restarted container is a NEW worker.
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

CLAIM_SQL = """
WITH picked AS (
    SELECT id FROM jobs
    WHERE status = 'queued'
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs j SET status = 'running',
                  attempt = j.attempt + 1,
                  worker_id = %(worker)s,
                  lease_until = now() + make_interval(secs => %(lease)s::double precision),
                  updated_at = now()
FROM picked
WHERE j.id = picked.id
RETURNING j.id, j.attempt, j.payload
"""

# Lease expired => the worker is presumed dead. Requeue, or finish the job if it can't be retried.
# FOR UPDATE SKIP LOCKED avoids deadlocks between several reapers touching many rows.
REAP_SQL = """
UPDATE jobs SET
    status = CASE WHEN cancel_requested THEN 'cancelled'
                  WHEN attempt >= %(max)s THEN 'failed'
                  ELSE 'queued' END,
    finished_at = CASE WHEN cancel_requested OR attempt >= %(max)s THEN now() ELSE NULL END,
    worker_id = NULL, lease_until = NULL, updated_at = now()
WHERE id IN (
    SELECT id FROM jobs
    WHERE status = 'running' AND lease_until < now()
    ORDER BY lease_until
    FOR UPDATE SKIP LOCKED
    LIMIT 200
)
"""

# Fenced: only the CURRENT attempt of a still-running job may write a result.
HEARTBEAT_SQL = """
UPDATE jobs SET lease_until = now() + make_interval(secs => %(lease)s::double precision),
                updated_at = now()
WHERE id = %(id)s AND attempt = %(attempt)s AND status = 'running'
RETURNING cancel_requested
"""

FINISH_OK_SQL = """
UPDATE jobs SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'succeeded' END,
                finished_at = now(), lease_until = NULL, updated_at = now()
WHERE id = %(id)s AND attempt = %(attempt)s AND status = 'running'
RETURNING status
"""

FINISH_FAIL_SQL = """
UPDATE jobs SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                finished_at = now(), lease_until = NULL, updated_at = now()
WHERE id = %(id)s AND attempt = %(attempt)s AND status = 'running'
RETURNING status
"""


def claim(pool):
    with pool.connection() as conn:
        return conn.execute(CLAIM_SQL, {"worker": WORKER_ID, "lease": LEASE_SECONDS}).fetchone()


def reap(pool) -> int:
    with pool.connection() as conn:
        return conn.execute(REAP_SQL, {"max": MAX_ATTEMPTS}).rowcount


def heartbeat(pool, job):
    """Returns cancel_requested (bool), or None if we lost the lease."""
    with pool.connection() as conn:
        row = conn.execute(HEARTBEAT_SQL, {"lease": LEASE_SECONDS, "id": job["id"],
                                           "attempt": job["attempt"]}).fetchone()
    return None if row is None else row["cancel_requested"]


def finish(pool, job, fail: bool):
    """Status change + effect row in ONE transaction. If the fenced UPDATE matches no row
    we are a zombie: write nothing. If the effect INSERT violated UNIQUE(job_id) the whole
    transaction rolls back (exception) - that would be a bug, and it is logged loudly."""
    params = {"id": job["id"], "attempt": job["attempt"]}
    with pool.connection() as conn:
        row = conn.execute(FINISH_FAIL_SQL if fail else FINISH_OK_SQL, params).fetchone()
        if row is None:
            return "fenced"
        if row["status"] == "succeeded":
            conn.execute(
                "INSERT INTO effects (job_id, attempt, worker_id) VALUES (%s, %s, %s)",
                (job["id"], job["attempt"], WORKER_ID),
            )
        return row["status"]


def run(pool, job):
    payload = job["payload"] or {}
    duration = payload.get("duration_ms", random.randint(WORK_MIN_MS, WORK_MAX_MS)) / 1000
    deadline = time.monotonic() + duration  # monotonic: only used for local pacing, not for leases
    next_hb = time.monotonic() + HEARTBEAT_SECONDS
    cancelled = False

    while time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= next_hb:
            cancel_requested = heartbeat(pool, job)
            if cancel_requested is None:
                log.warning("lost lease job=%s attempt=%s, abandoning", job["id"], job["attempt"])
                return
            if cancel_requested:
                cancelled = True
                break
            next_hb = time.monotonic() + HEARTBEAT_SECONDS

    if payload.get("freeze_s") and job["attempt"] <= int(payload.get("freeze_attempts", 1)):
        time.sleep(float(payload["freeze_s"]))  # no heartbeat on purpose; only the first attempt(s) freeze

    result = finish(pool, job, fail=bool(payload.get("fail")))
    if result == "fenced":
        log.warning("ZOMBIE write rejected job=%s attempt=%s", job["id"], job["attempt"])
    else:
        log.info("job=%s attempt=%s -> %s%s", job["id"], job["attempt"], result,
                 " (cancel observed)" if cancelled else "")


def reaper_loop(pool):
    """Runs in its own thread, so a worker that is busy with a long job still reaps expired leases."""
    while True:
        try:
            n = reap(pool)
            if n:
                log.info("reaper handled %d expired job(s)", n)
        except Exception:
            log.exception("reaper error")
        time.sleep(REAP_INTERVAL_SECONDS)


def main():
    log.info("worker %s starting (lease=%ss heartbeat=%ss)", WORKER_ID, LEASE_SECONDS, HEARTBEAT_SECONDS)
    pool = make_pool(max_size=3)
    threading.Thread(target=reaper_loop, args=(pool,), daemon=True).start()
    while True:
        try:
            job = claim(pool)
            if job is None:
                time.sleep(0.1 + random.random() * 0.2)
                continue
            run(pool, job)
        except Exception:
            # Never die on a DB hiccup or a bug: log and keep going. (kill -9 is the only way out.)
            log.exception("worker loop error")
            time.sleep(1)


if __name__ == "__main__":
    main()