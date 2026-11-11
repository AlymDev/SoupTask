"""Shared settings and connection pool. All timing decisions use the DB clock (now()),
never the clock of the API or a worker."""
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "30"))
HEARTBEAT_SECONDS = float(os.getenv("HEARTBEAT_SECONDS", "10"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))
WORK_MIN_MS = int(os.getenv("WORK_MIN_MS", "50"))
WORK_MAX_MS = int(os.getenv("WORK_MAX_MS", "500"))
REAP_INTERVAL_SECONDS = float(os.getenv("REAP_INTERVAL_SECONDS", "2"))

# Columns that are safe to show to API clients (payload_hash is internal).
PUBLIC_COLS = ("id, idempotency_key, payload, status, attempt, worker_id, lease_until, "
               "cancel_requested, created_at, updated_at, finished_at")


def make_pool(max_size: int = 10) -> ConnectionPool:
    # Default isolation level is READ COMMITTED. Every state change below is a single
    # conditional UPDATE, so correctness relies on row locks, not on isolation level.
    pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        timeout=30,
        open=True,
    )
    pool.wait(timeout=60)
    return pool