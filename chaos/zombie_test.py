#!/usr/bin/env python3
"""Zombie worker test: a worker freezes past its lease, wakes up, and tries to write a stale result.

    python chaos/zombie_test.py                                   # quick version, ~50 s
    python chaos/zombie_test.py --duration-ms 9000 --freeze-s 25  # "overlap" version, ~50 s

Quick version : attempt 2 is already FINISHED when the zombie wakes (the job status alone would stop it).
Overlap version: the zombie wakes while attempt 2 is still RUNNING, so only the attempt (fencing) check
                 can stop it. This is the version that tells fencing apart from the status check.
Expected end state: succeeded, attempt 2, exactly ONE effect and it comes from attempt 2,
and a "ZOMBIE write rejected ... attempt=1" log line.
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
    ap.add_argument("--freeze-s", type=float, default=40, help="how long the worker freezes after the work")
    ap.add_argument("--duration-ms", type=int, default=100, help="how long the work itself takes (every attempt)")
    ap.add_argument("--lease-s", type=float, default=30, help="LEASE_SECONDS of the workers")
    ap.add_argument("--label", default="", help="free text copied into the output file")
    args = ap.parse_args()
    dur = args.duration_ms / 1000

    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("zombie-*.txt") if (m := re.fullmatch(r"zombie-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"zombie-{n:02d}.txt")
    log(f"ZOMBIE TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}  "
        f"freeze_s={args.freeze_s} duration_ms={args.duration_ms}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()}  (deliberately broken worker; a FAIL is the expected result) ***")
    if args.label:
        log(f"label: {args.label}")

    since = h.psql("""SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")[0][0]
    key = f"zombie-{uuid.uuid4().hex[:8]}"
    code, job = h.http("POST", "/jobs", {"payload": {"duration_ms": args.duration_ms, "freeze_s": args.freeze_s}},
                       {"Idempotency-Key": key})
    if code != 201:
        log(f"could not create job: {code} {job}")
        sys.exit(2)
    jid = job["id"]
    log(f"created job {jid}")

    # wait long enough for: zombie wake-up, lease expiry + reaper + attempt 2 running to its end
    min_wait = max(args.freeze_s + 1 + dur, args.lease_s + 3 + dur) + 5
    t0, last, final = time.time(), None, None
    while time.time() - t0 < min_wait + 60:
        code, j = h.http("GET", f"/jobs/{jid}")
        if code == 200:
            cur = (j["status"], j["attempt"], (j["worker_id"] or "-"))
            if cur != last:
                log(f"[t={time.time() - t0:5.1f}s] status={cur[0]} attempt={cur[1]} worker={cur[2]}")
                last = cur
            final = j
            if j["status"] in TERMINAL and time.time() - t0 > min_wait:
                break
        time.sleep(1)

    _, logs, _ = h.sh(["docker", "compose", "logs", "--no-color", "--since", since, "worker"])
    lines = [l for l in logs.splitlines() if jid in l]
    log("\nworker log lines mentioning this job:")
    for l in lines:
        log("   " + l)
    effects = h.psql(f"SELECT attempt, worker_id FROM effects WHERE job_id = '{jid}'")
    log(f"\neffect rows for this job: {effects}")
    rejected = re.findall(r"ZOMBIE write rejected job=%s attempt=(\d+)" % re.escape(jid), logs)

    problems = []
    if not final or final["status"] != "succeeded":
        problems.append(f"final status is {final and final['status']}, expected succeeded")
    if not final or final["attempt"] < 2:
        problems.append("job was never re-claimed (attempt < 2): the lease did not expire, test is vacuous")
    if len(effects) != 1:
        problems.append(f"{len(effects)} effect rows, expected exactly 1")
    elif final and int(effects[0][0]) != final["attempt"]:
        problems.append(f"effect came from attempt {effects[0][0]}, final attempt is {final['attempt']} (stale write won)")
    if final and str(final["attempt"]) in rejected:
        problems.append(f"the CURRENT attempt ({final['attempt']}) was the one rejected: the stale write had already won")
    if "1" not in rejected:
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