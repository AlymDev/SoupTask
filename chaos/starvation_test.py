#!/usr/bin/env python3
"""Reaper starvation test: does a dead worker's job come back while ALL other workers are busy?

    python chaos/starvation_test.py        # about 2 min

8 jobs of 80s occupy all 8 workers. One busy worker is killed (kill -9) and NOT restarted
(a restarted worker would run the reaper at once and hide the problem).
In app/worker.py the reaper only runs between jobs, so with 7 busy workers nobody may call it.
Expected if the reaper is independent: the orphan leaves 'running' within lease + reap interval (+ margin) = 40s.
Expected if the reaper runs only between jobs: not before the other workers finish (about 80s).
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-s", type=float, default=80, help="length of every job; must be well above lease + bound")
    ap.add_argument("--bound-s", type=float, default=40, help="max seconds after the kill for the orphan to be recovered")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("starvation-*.txt") if (m := re.fullmatch(r"starvation-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"starvation-{n:02d}.txt")
    log(f"STARVATION TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}  {vars(args)}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()} ***")
    if args.label:
        log(f"label: {args.label}")

    _, out, _ = h.sh(["docker", "compose", "ps", "-q", "worker"])
    ids = out.split()
    log(f"running worker containers: {len(ids)} (expected {args.workers})")
    if len(ids) != args.workers:
        sys.exit("wrong number of workers; run `docker compose up -d` first")

    h.psql("TRUNCATE effects, jobs;")
    since = h.psql("""SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")[0][0]
    run_id = uuid.uuid4().hex[:8]
    for i in range(args.workers):
        code, job = h.http("POST", "/jobs", {"payload": {"duration_ms": int(args.work_s * 1000)}},
                           {"Idempotency-Key": f"starve-{run_id}-{i}"})
        if code != 201:
            sys.exit(f"could not create job {i}: {code} {job}")
    log(f"created {args.workers} jobs of {args.work_s:.0f}s")

    # wait until every worker holds a job
    t0 = time.time()
    running = []
    while time.time() - t0 < 30:
        code, body = h.http("GET", "/jobs?status=running&limit=50")
        running = body["items"] if code == 200 else []
        if len(running) == args.workers:
            break
        time.sleep(0.5)
    if len(running) != args.workers:
        sys.exit(f"only {len(running)} jobs running after 30s; test would be vacuous")
    log(f"all {args.workers} workers are busy")

    victim = running[0]
    prefix = victim["worker_id"].split("-")[0]
    cid = next((i for i in ids if i[:12] == prefix), None)
    if cid is None:
        sys.exit(f"could not map worker_id {victim['worker_id']} to a container")
    t_kill = time.time()
    h.sh(["docker", "kill", "--signal", "KILL", cid])
    log(f"KILL -9 container {cid[:12]} (worker {victim['worker_id']}) holding job {victim['id']} "
        f"(attempt {victim['attempt']}); NOT restarting it")

    recovered_t, last = None, None
    deadline = t_kill + args.work_s + 25
    while time.time() < deadline:
        code, j = h.http("GET", f"/jobs/{victim['id']}")
        if code == 200:
            cur = (j["status"], j["attempt"], j["worker_id"] or "-")
            if cur != last:
                log(f"[t=kill+{time.time() - t_kill:5.1f}s] orphan job: status={cur[0]} attempt={cur[1]} worker={cur[2]}")
                last = cur
            if j["status"] != "running" or j["attempt"] > victim["attempt"]:
                recovered_t = time.time() - t_kill
                break
        time.sleep(1)

    _, logs, _ = h.sh(["docker", "compose", "logs", "--no-color", "--since", since, "worker"])
    reaps = [l for l in logs.splitlines() if "reaper handled" in l]
    log(f"\nreaper log lines since start: {len(reaps)}")
    for l in reaps[:10]:
        log("   " + l)

    log("\ncleanup: restarting all workers, emptying tables")
    h.sh(["docker", "compose", "restart", "worker"])
    h.psql("TRUNCATE effects, jobs;")

    log("")
    if recovered_t is None:
        log(f"RESULT: FAIL: orphan job never left 'running' within {args.work_s + 25:.0f}s of the kill")
        sys.exit(1)
    log(f"orphan recovered {recovered_t:.1f}s after the kill (bound {args.bound_s:.0f}s, lease 30s)")
    if recovered_t > args.bound_s:
        log(f"RESULT: FAIL: recovery took {recovered_t:.1f}s > {args.bound_s:.0f}s. "
            f"While all other workers were busy, nobody ran the reaper.")
        sys.exit(1)
    log("RESULT: PASS (the dead worker's job was recovered while every other worker was busy)")


if __name__ == "__main__":
    main()