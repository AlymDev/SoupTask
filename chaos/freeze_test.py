#!/usr/bin/env python3
"""Freeze test: a worker is frozen (docker pause) DURING its work, longer than the lease.

    python chaos/freeze_test.py        # about 90 s

One job of 50s (lease 30s). About 3s after it starts running, its worker's container is paused
(everything in it stops: work, heartbeat, reaper thread). After the lease expires another worker's
reaper requeues the job and attempt 2 starts. At pause+40s the old worker is unpaused. It is still in
the middle of its work, so its next heartbeat must find the lease gone and abandon the job
("lost lease ... abandoning"). That branch never ran in any earlier test.
Expected end state: succeeded, attempt 2, exactly ONE effect (from attempt 2),
a "lost lease job=<id> attempt=1" log line, no effect from attempt 1.
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
    ap.add_argument("--work-s", type=float, default=50, help="length of the job (every attempt)")
    ap.add_argument("--pause-after-s", type=float, default=3, help="pause the worker this long after the job starts")
    ap.add_argument("--pause-s", type=float, default=40, help="how long the container stays paused; must exceed the lease")
    ap.add_argument("--lease-s", type=float, default=30)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    if args.pause_s <= args.lease_s:
        sys.exit("--pause-s must be longer than the lease, otherwise nothing expires")
    if args.pause_after_s + args.pause_s >= args.work_s:
        sys.exit("the old worker must wake BEFORE its own work ends, otherwise it never reaches a heartbeat")

    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("freeze-*.txt") if (m := re.fullmatch(r"freeze-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"freeze-{n:02d}.txt")
    log(f"FREEZE TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}  {vars(args)}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()}  (deliberately broken worker; a FAIL is the expected result) ***")
    if args.label:
        log(f"label: {args.label}")

    _, out, _ = h.sh(["docker", "compose", "ps", "-q", "worker"])
    ids = out.split()
    log(f"running worker containers: {len(ids)}")
    if len(ids) < 2:
        sys.exit("need at least 2 workers")

    h.psql("TRUNCATE effects, jobs;")
    since = h.psql("""SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")[0][0]
    code, job = h.http("POST", "/jobs", {"payload": {"duration_ms": int(args.work_s * 1000)}},
                       {"Idempotency-Key": f"freeze-{uuid.uuid4().hex[:8]}"})
    if code != 201:
        sys.exit(f"could not create job: {code} {job}")
    jid = job["id"]
    log(f"created job {jid} (work {args.work_s:.0f}s)")

    # wait until it runs, then pause its worker
    t0 = time.time()
    j = None
    while time.time() - t0 < 30:
        code, j = h.http("GET", f"/jobs/{jid}")
        if code == 200 and j["status"] == "running":
            break
        time.sleep(0.3)
    else:
        sys.exit("job never started running")
    t_run = time.time()
    time.sleep(args.pause_after_s)
    code, j = h.http("GET", f"/jobs/{jid}")
    victim_worker = j["worker_id"]
    prefix = victim_worker.split("-")[0]
    cid = next((i for i in ids if i[:12] == prefix), None)
    if cid is None:
        sys.exit(f"cannot map worker {victim_worker} to a container")

    paused = False
    final, last = None, None
    t_pause = t_unpause = None
    try:
        h.sh(["docker", "pause", cid])
        paused, t_pause = True, time.time()
        log(f"[t={t_pause - t_run:5.1f}s] PAUSED container {cid[:12]} (worker {victim_worker}) holding attempt {j['attempt']}")
        deadline = t_pause + args.pause_s + args.work_s + 40
        while time.time() < deadline:
            now = time.time()
            if paused and now - t_pause >= args.pause_s:
                h.sh(["docker", "unpause", cid])
                paused, t_unpause = False, now
                log(f"[t={now - t_run:5.1f}s] UNPAUSED container {cid[:12]}")
            code, j = h.http("GET", f"/jobs/{jid}")
            if code == 200:
                cur = (j["status"], j["attempt"], j["worker_id"] or "-")
                if cur != last:
                    log(f"[t={time.time() - t_run:5.1f}s] status={cur[0]} attempt={cur[1]} worker={cur[2]}")
                    last = cur
                final = j
                if j["status"] in TERMINAL and t_unpause is not None:
                    break
            time.sleep(1)
    finally:
        if paused:
            h.sh(["docker", "unpause", cid])
            log("(unpaused in cleanup)")

    time.sleep(2)
    _, logs, _ = h.sh(["docker", "compose", "logs", "--no-color", "--since", since, "worker"])
    lines = [l for l in logs.splitlines() if jid in l]
    log("\nworker log lines mentioning this job:")
    for l in lines:
        log("   " + l)
    effects = h.psql(f"SELECT attempt, worker_id FROM effects WHERE job_id = '{jid}'")
    log(f"\neffect rows: {effects}")

    lost = [l for l in lines if "lost lease" in l and "attempt=1" in l]
    zombie = [l for l in lines if "ZOMBIE" in l]
    problems = []
    if not final or final["status"] != "succeeded":
        problems.append(f"final status is {final and final['status']}, expected succeeded")
    if not final or final["attempt"] < 2:
        problems.append("job was never re-claimed (attempt < 2): the lease did not expire, test is vacuous")
    if len(effects) != 1:
        problems.append(f"{len(effects)} effect rows, expected exactly 1")
    elif final and int(effects[0][0]) != final["attempt"]:
        problems.append(f"effect came from attempt {effects[0][0]}, final attempt is {final['attempt']}")
    if not lost:
        problems.append("no 'lost lease ... attempt=1' line: the frozen worker did not notice at its heartbeat that it lost the job"
                        + (f" (it logged ZOMBIE instead: {len(zombie)} line(s))" if zombie else ""))
    log("")
    if problems:
        log("RESULT: FAIL")
        for p in problems:
            log("  - " + p)
        sys.exit(1)
    log("RESULT: PASS (frozen worker woke mid-work, found its lease gone, abandoned; one effect from attempt 2)")


if __name__ == "__main__":
    main()