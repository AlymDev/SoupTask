#!/usr/bin/env python3
"""Heartbeat test: the two things the heartbeat is for.

    python chaos/heartbeat_test.py          # about 55 s

A "long job": a 45 s job with a 30 s lease. Only the heartbeat (every 10 s) keeps the lease alive.
              Expected: never re-claimed (attempt stays 1), lease_until moves forward, succeeded, one effect.
B "cancel"  : a 60 s job, cancelled 3 s after it starts running (HTTP 202 = "cancel accepted").
              Expected: the worker notices at its next heartbeat and stops: cancelled within
              heartbeat + 3 s (not after 60 s), and no effect row.
Both jobs run at the same time. The chaos runs never exercised this: their jobs are shorter than the heartbeat.
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


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--long-s", type=float, default=45, help="length of job A; must be longer than the lease")
    ap.add_argument("--cancel-after-s", type=float, default=3, help="cancel job B this long after it starts running")
    ap.add_argument("--lease-s", type=float, default=30, help="LEASE_SECONDS of the workers")
    ap.add_argument("--hb-s", type=float, default=10, help="HEARTBEAT_SECONDS of the workers")
    ap.add_argument("--label", default="", help="free text copied into the output file")
    args = ap.parse_args()
    if args.long_s <= args.lease_s:
        sys.exit("--long-s must be longer than the lease, otherwise job A proves nothing")

    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("heartbeat-*.txt") if (m := re.fullmatch(r"heartbeat-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"heartbeat-{n:02d}.txt")
    log(f"HEARTBEAT TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}  {vars(args)}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()}  (deliberately broken worker; a FAIL is the expected result) ***")
    if args.label:
        log(f"label: {args.label}")

    since = h.psql("""SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")[0][0]
    run_id = uuid.uuid4().hex[:8]
    jobs = {}
    for name, seconds in (("A_long", args.long_s), ("B_cancel", 60)):
        code, job = h.http("POST", "/jobs", {"payload": {"duration_ms": int(seconds * 1000)}},
                           {"Idempotency-Key": f"hb-{name}-{run_id}"})
        if code != 201:
            log(f"could not create job {name}: {code} {job}")
            sys.exit(2)
        jobs[name] = {"id": job["id"], "last": None, "final": None, "done": False, "done_t": None,
                      "first_running": None, "leases": [], "requeued": False}
        log(f"created {name}: {job['id']}  (work takes {seconds:.0f}s)")

    t0 = time.time()
    cancel_t = cancel_code = None
    deadline = t0 + max(args.long_s, 60) + 30
    while time.time() < deadline:
        now = time.time() - t0
        for name, st in jobs.items():
            if st["done"]:
                continue
            code, j = h.http("GET", f"/jobs/{st['id']}")
            if code != 200:
                continue
            st["final"] = j
            cur = (j["status"], j["attempt"])
            if cur != st["last"]:
                log(f"[t={now:5.1f}s] {name}: status={j['status']} attempt={j['attempt']} worker={j['worker_id'] or '-'}")
                st["last"] = cur
            if j["status"] == "running":
                if st["first_running"] is None:
                    st["first_running"] = now
                if j["lease_until"] and j["attempt"] == 1:   # a new attempt gets a fresh lease: not an extension
                    st["leases"].append((now, parse(j["lease_until"])))
            if j["status"] in TERMINAL:
                st["done"], st["done_t"] = True, now
            if name == "A_long" and j["attempt"] > 1:   # re-claimed while its worker is alive: already a failure
                st["done"], st["done_t"], st["requeued"] = True, now, True
        b = jobs["B_cancel"]
        if cancel_t is None and not b["done"] and b["first_running"] is not None \
                and now - b["first_running"] >= args.cancel_after_s:
            cancel_code, _ = h.http("POST", f"/jobs/{b['id']}/cancel")
            cancel_t = time.time() - t0
            log(f"[t={cancel_t:5.1f}s] B_cancel: POST cancel -> HTTP {cancel_code}")
        if all(st["done"] for st in jobs.values()):
            break
        time.sleep(0.5)

    _, logs, _ = h.sh(["docker", "compose", "logs", "--no-color", "--since", since, "worker"])
    for name, st in jobs.items():
        lines = [l for l in logs.splitlines() if st["id"] in l]
        log(f"\nworker log lines for {name}:")
        for l in lines:
            log("   " + l)
        st["effects"] = h.psql(f"SELECT attempt FROM effects WHERE job_id = '{st['id']}'")
        st["warn"] = [l for l in lines if "ZOMBIE" in l or "lost lease" in l]
        log(f"effect rows for {name}: {st['effects']}")

    problems = []
    a, b = jobs["A_long"], jobs["B_cancel"]
    fa, fb = a["final"], b["final"]

    log("")
    if a["leases"]:
        rise = (a["leases"][-1][1] - a["leases"][0][1]).total_seconds()
        log(f"A: lease_until moved forward by {rise:.1f}s between first and last observation "
            f"(heartbeat every {args.hb_s:.0f}s)")
        if rise < args.hb_s * 0.5:
            problems.append("A: lease_until never moved, so the heartbeat did not extend the lease")
    else:
        problems.append("A: never observed the job running with a lease")
    if not fa or fa["status"] != "succeeded":
        problems.append(f"A: final status is {fa and fa['status']}, expected succeeded")
    if a["requeued"] or (fa and fa["attempt"] != 1):
        problems.append(f"A: job was re-claimed (attempt {fa and fa['attempt']}) while its worker was alive: "
                        f"the lease expired during a {args.long_s:.0f}s job")
    if len(a["effects"]) != 1:
        problems.append(f"A: {len(a['effects'])} effect rows, expected exactly 1")
    if a["warn"]:
        problems.append(f"A: worker logged stale-write/lost-lease lines: {len(a['warn'])}")

    if cancel_code != 202:
        problems.append(f"B: cancel returned HTTP {cancel_code}, expected 202 (the job should have been running)")
    if not fb or fb["status"] != "cancelled":
        problems.append(f"B: final status is {fb and fb['status']}, expected cancelled")
    if cancel_t is not None and b["done_t"] is not None:
        latency = b["done_t"] - cancel_t
        bound = args.hb_s + 3
        log(f"B: cancel accepted at t={cancel_t:.1f}s, job terminal at t={b['done_t']:.1f}s -> "
            f"latency {latency:.1f}s (bound {bound:.0f}s = heartbeat + 3s)")
        if latency > bound:
            problems.append(f"B: cancel took {latency:.1f}s to take effect (bound {bound:.0f}s): "
                            f"the worker did not notice the cancel at its heartbeat")
    if b["effects"]:
        problems.append(f"B: cancelled job has {len(b['effects'])} effect row(s), expected 0")

    log("")
    if problems:
        log("RESULT: FAIL")
        for p in problems:
            log("  - " + p)
        sys.exit(1)
    log("RESULT: PASS (45s job survived a 30s lease; running job was cancelled within one heartbeat interval)")


if __name__ == "__main__":
    main()