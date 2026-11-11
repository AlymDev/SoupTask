#!/usr/bin/env python3
"""Load test: ~100 requests/s of a mixed workload against the API, no kills.

    python chaos/load_test.py                       # 60 s at 100 req/s, jobs of 50 ms
    python chaos/load_test.py --rate 200            # 10x-style probe

Mix: 50% create, 30% GET /jobs/{id}, 10% GET /jobs (list), 10% cancel.
Requests are sent on a fixed schedule (open loop). Latency = response time minus the time the
request SHOULD have been sent, so a slow server cannot hide by slowing the test down.
After the load stops it waits for the queue to drain and reports how long that took.
Limits: client, API, workers and Postgres all run on one machine (Docker Desktop), client is Python threads.
"""
import argparse
import random
import re
import sys
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as h  # reuse http / sh / psql / Tee

MIX = (("create", 0.5), ("get", 0.3), ("list", 0.1), ("cancel", 0.1))
OK = {"create": (200, 201), "get": (200,), "list": (200,), "cancel": (200, 202)}


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=100, help="requests per second")
    ap.add_argument("--duration-s", type=float, default=60)
    ap.add_argument("--work-ms", type=int, default=50, help="simulated work per job")
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--drain-timeout-s", type=float, default=180)
    ap.add_argument("--p99-ms", type=float, default=500, help="my own threshold for the API p99 (arbitrary, adjust and say so)")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("load-*.txt") if (m := re.fullmatch(r"load-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"load-{n:02d}.txt")
    log(f"LOAD TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}  {vars(args)}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()} ***")
    if args.label:
        log(f"label: {args.label}")
    n_workers = len(h.sh(["docker", "compose", "ps", "-q", "worker"])[1].split())
    log(f"running worker containers: {n_workers}; git commit: {h.sh(['git', 'rev-parse', '--short', 'HEAD'])[1] or 'n/a'}")
    if h.http("GET", "/healthz")[0] != 200:
        sys.exit("API not healthy")

    h.psql("TRUNCATE effects, jobs;")
    run_id = uuid.uuid4().hex[:6]
    lock = threading.Lock()
    rows = []            # (op, code, latency_s, lag_s)
    ids = []
    stop_mon = threading.Event()
    samples = []         # (t, queued, running)
    T0 = time.time()

    def monitor():
        while not stop_mon.wait(5):
            try:
                c = {s: int(k) for s, k in h.psql("SELECT status, count(*) FROM jobs GROUP BY status")}
            except Exception:
                continue
            samples.append((time.time() - T0, c.get("queued", 0), c.get("running", 0)))
            log(f"[t={time.time() - T0:5.1f}s] queued={c.get('queued', 0)} running={c.get('running', 0)} "
                f"succeeded={c.get('succeeded', 0)} cancelled={c.get('cancelled', 0)} failed={c.get('failed', 0)}")

    def do(i, target):
        start = time.time()
        r = random.random()
        acc = 0
        op = "get"
        for name, w in MIX:
            acc += w
            if r < acc:
                op = name
                break
        with lock:
            known = random.choice(ids) if ids else None
        if op in ("get", "cancel") and known is None:
            op = "create"
        if op == "create":
            code, body = h.http("POST", "/jobs", {"payload": {"duration_ms": args.work_ms}},
                                {"Idempotency-Key": f"load-{run_id}-{i}"})
            if code in (200, 201) and isinstance(body, dict):
                with lock:
                    ids.append(body["id"])
        elif op == "get":
            code, _ = h.http("GET", f"/jobs/{known}")
        elif op == "list":
            code, _ = h.http("GET", "/jobs?limit=50")
        else:
            code, _ = h.http("POST", f"/jobs/{known}/cancel")
        end = time.time()
        with lock:
            rows.append((op, code, end - target, start - target))

    threading.Thread(target=monitor, daemon=True).start()
    total = int(args.rate * args.duration_s)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for i in range(total):
            target = t0 + i / args.rate
            wait = target - time.time()
            if wait > 0:
                time.sleep(wait)
            ex.submit(do, i, target)
    t_end = time.time()
    log(f"[t={t_end - T0:5.1f}s] load stopped: {total} requests scheduled over {t_end - t0:.1f}s "
        f"(achieved send rate {total / (t_end - t0):.1f}/s)")

    # drain
    t_drain0 = time.time()
    drained = None
    while time.time() - t_drain0 < args.drain_timeout_s:
        c = {s: int(k) for s, k in h.psql("SELECT status, count(*) FROM jobs GROUP BY status")}
        if c.get("queued", 0) + c.get("running", 0) == 0:
            drained = time.time() - t_drain0
            break
        time.sleep(2)
    stop_mon.set()

    # report
    log("\n=== LATENCY (ms, from scheduled time) ===")
    by = defaultdict(list)
    errs = defaultdict(int)
    for op, code, lat, lag in rows:
        by[op].append(lat * 1000)
        if code not in OK[op]:
            errs[(op, code)] += 1
    allrows = [lat * 1000 for _, _, lat, _ in rows]
    for op in ("create", "get", "list", "cancel"):
        xs = by[op]
        log(f"{op:7s} n={len(xs):5d}  p50={pct(xs, 50):7.1f}  p95={pct(xs, 95):7.1f}  p99={pct(xs, 99):7.1f}  max={max(xs, default=float('nan')):7.1f}")
    p99 = pct(allrows, 99)
    log(f"ALL     n={len(allrows):5d}  p50={pct(allrows, 50):7.1f}  p95={pct(allrows, 95):7.1f}  p99={p99:7.1f}")
    log(f"client dispatch lag (request started minus scheduled): p95={pct([r[3] * 1000 for r in rows], 95):.1f} ms, "
        f"max={max((r[3] for r in rows), default=0) * 1000:.1f} ms  (large => the Python client itself is the bottleneck)")
    log(f"errors / unexpected status codes: {dict(errs) or 'none'}")

    peak_q = max((q for _, q, _ in samples), default=0)
    log(f"\nqueue: peak queued (sampled every 5s) = {peak_q}; "
        + (f"drained {drained:.1f}s after the load stopped" if drained is not None
           else f"NOT drained within {args.drain_timeout_s:.0f}s"))

    created = len(ids)
    jobs_n = int(h.psql("SELECT count(*) FROM jobs")[0][0])
    dbl = h.psql("SELECT job_id, count(*) FROM effects GROUP BY job_id HAVING count(*) > 1")
    stuck = h.psql("SELECT id FROM jobs WHERE status IN ('queued','running')")
    log(f"integrity: jobs in DB={jobs_n}, creates confirmed={created}, double effects={len(dbl)}, still queued/running={len(stuck)}")

    problems = []
    if errs:
        problems.append(f"unexpected responses: {dict(errs)}")
    if p99 > args.p99_ms:
        problems.append(f"overall p99 {p99:.0f} ms > threshold {args.p99_ms:.0f} ms (threshold is my own choice)")
    if drained is None:
        problems.append("queue did not drain")
    if dbl or stuck or jobs_n != created:
        problems.append("integrity check failed")
    log("")
    if problems:
        log("RESULT: FAIL")
        for p in problems:
            log("  - " + p)
        sys.exit(1)
    log("RESULT: PASS (on this machine, this mix, this duration; not a capacity proof)")


if __name__ == "__main__":
    main()