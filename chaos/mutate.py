#!/usr/bin/env python3
"""Mutation testing: deliberately BREAK the worker, then check that our tests notice.

    python chaos/mutate.py list
    python chaos/mutate.py apply <name>    # patches app/worker.py, rebuilds the containers
    python chaos/mutate.py restore         # puts the original worker.py (and the DB constraint) back

While a mutation is active, app/.mutation exists and every run file says "MUTATION ACTIVE" at the top,
so a deliberate failure can never be mistaken for a real one. worker.py is backed up as worker.py.orig.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as h  # reuse sh / psql / http

ROOT = h.ROOT
WORKER = ROOT / "app" / "worker.py"
BACKUP = ROOT / "app" / "worker.py.orig"
MARK = ROOT / "app" / ".mutation"
EXPECTED_WORKERS = 8

FENCE = "WHERE id = %(id)s AND attempt = %(attempt)s AND status = 'running'"

MUTATIONS = {
    "no_attempt_check": {
        "desc": "writes are no longer fenced by the attempt number (the status='running' check stays)",
        "edits": [(FENCE, "WHERE id = %(id)s AND status = 'running'")],
        "drop_unique": False,
        "test": 'python chaos/zombie_test.py --duration-ms 9000 --freeze-s 25 --label "mutation no_attempt_check"',
        "expect": "FAIL: the stale attempt-1 write wins (effect from attempt 1, current attempt rejected)",
    },
    "no_fencing_at_all": {
        "desc": "no attempt check AND no status check on writes, and UNIQUE(job_id) on effects is dropped",
        "edits": [(FENCE, "WHERE id = %(id)s")],
        "drop_unique": True,
        "test": 'python chaos/zombie_test.py --duration-ms 9000 --freeze-s 25 --label "mutation no_fencing_at_all"',
        "expect": "FAIL: 2 effect rows for one job (the double side effect the task asks us to count)",
    },
    "plain_claim": {
        "desc": "claim without FOR UPDATE SKIP LOCKED (plain SELECT then UPDATE)",
        "edits": [("    ORDER BY created_at, id\n    FOR UPDATE SKIP LOCKED\n    LIMIT 1",
                   "    ORDER BY created_at, id\n    LIMIT 1")],
        "drop_unique": False,
        "test": "python chaos/run.py --jobs 1000 --no-kill",
        "expect": "FAIL no_double_claim_without_kills: jobs with attempt 2 although nothing was killed",
    },
    "ignore_cancel": {
        "desc": "finishing a job ignores cancel_requested (always 'succeeded')",
        "edits": [("status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'succeeded' END,",
                   "status = 'succeeded',")],
        "drop_unique": False,
        "test": "python chaos/run.py --jobs 1000 --no-kill",
        "expect": "FAIL cancel_is_honoured: cancels accepted (HTTP 202) but the job ended 'succeeded'",
    },
    "no_heartbeat": {
        "desc": "workers never send a heartbeat: the lease is never extended and cancels are not noticed during a job",
        "edits": [("if time.monotonic() >= next_hb:", "if False:  # MUTATION: heartbeat disabled")],
        "drop_unique": False,
        "test": 'python chaos/heartbeat_test.py --label "mutation no_heartbeat"',
        "expect": "FAIL: the 45s job is re-claimed after ~30s; the cancelled 60s job is only finished by the reaper after the lease expires",
    },
    "no_cancel_in_heartbeat": {
        "desc": "the heartbeat still extends the lease, but the worker ignores cancel_requested during the job",
        "edits": [("            if cancel_requested:\n                cancelled = True\n                break",
                   "            if False:  # MUTATION: cancel flag ignored during the job\n                cancelled = True\n                break")],
        "drop_unique": False,
        "test": 'python chaos/heartbeat_test.py --label "mutation no_cancel_in_heartbeat"',
        "expect": "FAIL: a cancelled 60s job only stops when it finishes (~57s after the cancel instead of within ~13s)",
    },
    "no_reaper": {
        "desc": "the reaper never runs, so expired leases are never recovered",
        "edits": [("n = reap(pool)", "n = 0  # MUTATION: reaper disabled")],
        "drop_unique": False,
        "test": "python chaos/run.py --jobs 500 --timeout 150",
        "expect": "FAIL none_stuck + settled_before_timeout: jobs of killed workers stay 'running' forever",
    },
        "clock_behind": {
        "desc": "claim computes lease_until from the WORKER clock, which is 40s behind (lease 30s => already expired)",
        "edits": [
            ("import uuid\n", "import uuid\nimport datetime as _dt\n"),
            ("lease_until = now() + make_interval(secs => %(lease)s::double precision),\n                  updated_at = now()\nFROM picked",
             "lease_until = %(lease_until)s,\n                  updated_at = now()\nFROM picked"),
            ('return conn.execute(CLAIM_SQL, {"worker": WORKER_ID, "lease": LEASE_SECONDS}).fetchone()',
             'return conn.execute(CLAIM_SQL, {"worker": WORKER_ID, "lease": LEASE_SECONDS, '
             '"lease_until": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=40) '
             '+ _dt.timedelta(seconds=LEASE_SECONDS)}).fetchone()'),
        ],
        "drop_unique": False,
        "test": "python chaos/run.py --jobs 1000 --no-kill",
        "expect": "hypothesis: reaper requeues jobs that are still running => attempt>1 without kills, ZOMBIE rejections (no_double_claim_without_kills / no_fenced_writes fail); effects should stay unique thanks to fencing",
    },
}

RESTORE_UNIQUE_SQL = """
TRUNCATE effects, jobs;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'effects_job_id_key') THEN
    ALTER TABLE effects ADD CONSTRAINT effects_job_id_key UNIQUE (job_id);
  END IF;
END $$;
"""


def mutated_source(name, src):
    for old, new in MUTATIONS[name]["edits"]:
        if old not in src:
            sys.exit(f"cannot find this text in app/worker.py, so I cannot apply '{name}':\n{old}\n"
                     f"(worker.py differs from what mutate.py expects; tell me and we fix the pattern)")
        src = src.replace(old, new)
    return src


def rebuild():
    print("rebuilding images and restarting containers (about 1 minute)...", flush=True)
    if subprocess.run(["docker", "compose", "up", "--build", "-d"], cwd=ROOT).returncode:
        sys.exit("docker compose up --build failed")
    for _ in range(90):
        _, out, _ = h.sh(["docker", "compose", "ps", "-q", "worker"])
        if len(out.split()) == EXPECTED_WORKERS and h.http("GET", "/healthz")[0] == 200:
            break
        time.sleep(1)
    time.sleep(8)  # let the workers finish starting
    _, out, _ = h.sh(["docker", "compose", "ps", "-q", "worker"])
    print(f"running worker containers: {len(out.split())} (expected {EXPECTED_WORKERS})")


def restore(do_rebuild=True):
    if BACKUP.exists():
        shutil.copyfile(BACKUP, WORKER)
        BACKUP.unlink()
        print("app/worker.py restored from worker.py.orig")
    if MARK.exists():
        MARK.unlink()
    h.psql(RESTORE_UNIQUE_SQL)  # also empties jobs/effects: save any evidence BEFORE running restore
    print("database: tables emptied, UNIQUE(job_id) on effects present")
    if do_rebuild:
        rebuild()
        print("restored. Check `git status` shows no change to app/worker.py.")


def apply(name):
    m = MUTATIONS[name]
    restore(do_rebuild=False)  # never stack mutations
    shutil.copyfile(WORKER, BACKUP)
    WORKER.write_text(mutated_source(name, WORKER.read_text(encoding="utf-8")), encoding="utf-8")
    MARK.write_text(name)
    if m["drop_unique"]:
        h.psql("ALTER TABLE effects DROP CONSTRAINT IF EXISTS effects_job_id_key;")
        print("database: UNIQUE(job_id) on effects DROPPED")
    rebuild()
    print(f"\nMUTATION '{name}' is active: {m['desc']}")
    print(f"NEXT   : {m['test']}")
    print(f"EXPECT : {m['expect']}")
    print("AFTER  : save the output (it is already in RUNS/), then run: python chaos/mutate.py restore")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for k, m in MUTATIONS.items():
            print(f"{k}\n   breaks : {m['desc']}\n   test   : {m['test']}\n   expect : {m['expect']}\n")
    elif len(sys.argv) == 3 and sys.argv[1] == "apply" and sys.argv[2] in MUTATIONS:
        apply(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "restore":
        restore()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()