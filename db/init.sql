-- Schema for the job queue. Runs once, on first start of an empty Postgres volume.
-- To re-run it: docker compose down -v && docker compose up

CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key  text        NOT NULL,
    payload          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    payload_hash     text        NOT NULL,          -- same key + different body => 409
    status           text        NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    attempt          integer     NOT NULL DEFAULT 0, -- fencing token, +1 on every claim
    worker_id        text,
    lease_until      timestamptz,
    cancel_requested boolean     NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,

    CONSTRAINT jobs_idempotency_key_uniq UNIQUE (idempotency_key),

    -- A running job must have a lease and a worker; others must not look "running".
    CONSTRAINT jobs_running_has_lease
        CHECK (status <> 'running' OR (lease_until IS NOT NULL AND worker_id IS NOT NULL)),
    -- Terminal states must have finished_at.
    CONSTRAINT jobs_terminal_has_finished_at
        CHECK (status NOT IN ('succeeded','failed','cancelled') OR finished_at IS NOT NULL)
);

-- The side effect: one row = one real execution. UNIQUE(job_id) is what enforces
-- "at most one effect per job" even if our application logic has a bug.
CREATE TABLE effects (
    id         bigserial   PRIMARY KEY,
    job_id     uuid        NOT NULL UNIQUE REFERENCES jobs(id),
    attempt    integer     NOT NULL,
    worker_id  text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Claiming: find the oldest queued job quickly.
CREATE INDEX jobs_queued_idx  ON jobs (created_at, id) WHERE status = 'queued';
-- Reaper: find expired leases quickly.
CREATE INDEX jobs_running_idx ON jobs (lease_until)    WHERE status = 'running';
-- Listing with keyset pagination.
CREATE INDEX jobs_list_idx    ON jobs (created_at DESC, id DESC);