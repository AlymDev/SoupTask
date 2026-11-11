#!/usr/bin/env python3
"""Zombie worker test: a worker freezes past its lease, wakes up, and tries to write a stale result.

    python chaos/zombie_test.py            # takes about 1 minute (lease is 30s)

Timeline it creates:
  t=0    worker A claims the job (attempt 1) and finishes the work in 100 ms, then FREEZES for 40 s
         (no heartbeat, like a stopped process or a long GC pause)
  t=30s  lease expires, a reaper requeues the job, worker B claims it (attempt 2) and finishes it
  t=40s  worker A wakes up and tries to commit attempt 1 => must be rejected ("ZOMBIE write rejected")
Expected end state: succeeded, attempt 2, exactly ONE effect and it comes from attempt 2.
Needs the worker image built with the freeze_attempts change: docker compose up --build
"""
import argparse
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as h  # reuse http / sh / psql / Tee

TERMINAL = ("succeeded", "failed", "cancelled")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze-s", type=float, default=40, help="must be longer than LEASE_SECONDS (30) + ~3s")
    args = ap.parse_args()

    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("zombie-*.txt") if (m := re.fullmatch(r"zombie-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"zombie-{n:02d}.txt")
    log(f"ZOMBIE TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}  freeze_s={args.freeze_s}")

    since = h.psql("""SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")[0][0]
    key = f"zombie-{uuid.uuid4().hex[:8]}"
    code, job = h.http("POST", "/jobs", {"payload": {"duration_ms": 100, "freeze_s": args.freeze_s}},
                       {"Idempotency-Key": key})
    if code != 201:
        log(f"could not create job: {code} {job}")
        sys.exit(2)
    jid = job["id"]
    log(f"created job {jid}")

    t0, last, final = time.time(), None, None
    while time.time() - t0 < args.freeze_s + 60:
        code, j = h.http("GET", f"/jobs/{jid}")
        if code == 200:
            cur = (j["status"], j["attempt"], (j["worker_id"] or "-"))
            if cur != last:
                log(f"[t={time.time() - t0:5.1f}s] status={cur[0]} attempt={cur[1]} worker={cur[2]}")
                last = cur
            final = j
            if j["status"] in TERMINAL and time.time() - t0 > args.freeze_s + 3:
                break  # the zombie has woken up and had time to try its write
        time.sleep(1)

    _, logs, _ = h.sh(["docker", "compose", "logs", "--no-color", "--since", since, "worker"])
    lines = [l for l in logs.splitlines() if jid in l]
    log("\nworker log lines mentioning this job:")
    for l in lines:
        log("   " + l)
    effects = h.psql(f"SELECT attempt, worker_id FROM effects WHERE job_id = '{jid}'")
    log(f"\neffect rows for this job: {effects}")

    problems = []
    if not final or final["status"] != "succeeded":
        problems.append(f"final status is {final and final['status']}, expected succeeded")
    if not final or final["attempt"] < 2:
        problems.append("job was never re-claimed (attempt < 2): the lease did not expire, test is vacuous")
    if len(effects) != 1:
        problems.append(f"{len(effects)} effect rows, expected exactly 1")
    elif final and int(effects[0][0]) != final["attempt"]:
        problems.append(f"effect came from attempt {effects[0][0]}, final attempt is {final['attempt']} (stale write won)")
    if not any("ZOMBIE write rejected" in l and "attempt=1" in l for l in lines):
        problems.append("no 'ZOMBIE write rejected ... attempt=1' line: the old worker's write was not seen being rejected")
    log("")
    if problems:
        log("RESULT: FAIL")
        for p in problems:
            log("  - " + p)
        sys.exit(1)
    log("RESULT: PASS (stale attempt 1 was rejected by the attempt check, one effect from attempt 2)")


if __name__ == "__main__":
    main()