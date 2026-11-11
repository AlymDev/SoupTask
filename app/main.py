import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .db import PUBLIC_COLS, make_pool

pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = make_pool(max_size=20)
    yield
    pool.close()


app = FastAPI(title="Soup job queue", lifespan=lifespan)


class JobIn(BaseModel):
    payload: dict = Field(default_factory=dict)


def respond(row, status_code=200):
    return JSONResponse(content=jsonable_encoder(row), status_code=status_code)


@app.get("/healthz")
def healthz():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.post("/jobs")
def create_job(body: JobIn, idempotency_key: str = Header(..., alias="Idempotency-Key",
                                                          min_length=1, max_length=200)):
    payload_hash = hashlib.sha256(
        json.dumps(body.payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pool.connection() as conn:
        # Uniqueness is enforced by the DB (UNIQUE idempotency_key), so concurrent duplicate
        # POSTs are safe: the loser of the race blocks until the winner commits, then no-ops.
        row = conn.execute(
            f"""INSERT INTO jobs (idempotency_key, payload, payload_hash)
                VALUES (%s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING {PUBLIC_COLS}""",
            (idempotency_key, Jsonb(body.payload), payload_hash),
        ).fetchone()
        if row:
            return respond(row, 201)
        # Key already existed. Separate statement => fresh READ COMMITTED snapshot => sees it.
        existing = conn.execute(
            f"SELECT {PUBLIC_COLS}, payload_hash FROM jobs WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone()
    if existing["payload_hash"] != payload_hash:
        raise HTTPException(409, "Idempotency-Key already used with a different payload")
    existing.pop("payload_hash")
    return respond(existing, 200)


@app.get("/jobs/{job_id}")
def get_job(job_id: UUID):
    with pool.connection() as conn:
        row = conn.execute(f"SELECT {PUBLIC_COLS} FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return respond(row)


def _encode_cursor(created_at: datetime, job_id) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{job_id}".encode()).decode()


def _decode_cursor(cursor: str):
    try:
        ts, jid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), UUID(jid)
    except Exception:
        raise HTTPException(400, "invalid cursor")


@app.get("/jobs")
def list_jobs(limit: int = Query(50, ge=1, le=200), cursor: Optional[str] = None,
              status: Optional[Literal["queued", "running", "succeeded", "failed", "cancelled"]] = None):
    """Keyset pagination, newest first. Offset pagination would skip/repeat rows while jobs
    are being inserted. Known limit: created_at is the transaction start time, so a row that
    commits late can appear 'behind' a cursor that was already handed out."""
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    if cursor:
        ts, jid = _decode_cursor(cursor)
        where.append("(created_at, id) < (%s, %s)")
        params += [ts, jid]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {PUBLIC_COLS} FROM jobs {clause} ORDER BY created_at DESC, id DESC LIMIT %s",
            params + [limit + 1],
        ).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
    return respond({"items": rows, "next_cursor": next_cursor})


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: UUID):
    """One atomic statement, so cancel cannot race with a claim or a reaper requeue:
       queued  -> cancelled immediately
       running -> cancel_requested = true (worker or reaper finishes the cancellation)
       terminal -> untouched (no match), we just return the current state."""
    with pool.connection() as conn:
        row = conn.execute(
            f"""UPDATE jobs SET
                    status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                    finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
                    cancel_requested = CASE WHEN status = 'running' THEN true ELSE cancel_requested END,
                    updated_at = now()
                WHERE id = %s AND status IN ('queued', 'running')
                RETURNING {PUBLIC_COLS}""",
            (job_id,),
        ).fetchone()
        if row:
            return respond(row, 202 if row["status"] == "running" else 200)
        row = conn.execute(f"SELECT {PUBLIC_COLS} FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return respond(row, 200)  # already terminal: idempotent no-op