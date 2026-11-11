#!/usr/bin/env python3
"""A job that is going to FAIL is cancelled while running (HTTP 202). Which final state does it get?

    python chaos/fail_cancel_test.py     # about 20 s
Expected: cancelled (the API said "cancel accepted"), no effect row.
My hypothesis from reading app/worker.py: FINISH_FAIL_SQL ignores cancel_requested, so the job ends 'failed'.
"""
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as h

TERMINAL = ("succeeded", "failed", "cancelled")


def main():
    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("failcancel-*.txt") if (m := re.fullmatch(r"failcancel-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"failcancel-{n:02d}.txt")
    log(f"FAIL+CANCEL TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()} ***")
    h.psql("TRUNCATE effects, jobs;")
    code, job = h.http("POST", "/jobs", {"payload": {"duration_ms": 15000, "fail": True}},
                       {"Idempotency-Key": f"fc-{uuid.uuid4().hex[:8]}"})
    if code != 201:
        sys.exit(f"create failed: {code} {job}")
    jid = job["id"]
    t0 = time.time()
    while time.time() - t0 < 20:
        code, j = h.http("GET", f"/jobs/{jid}")
        if code == 200 and j["status"] == "running":
            break
        time.sleep(0.3)
    else:
        sys.exit("never running")
    time.sleep(3)
    cancel_code, body = h.http("POST", f"/jobs/{jid}/cancel")
    log(f"cancel -> HTTP {cancel_code}, status in response: {body and body.get('status')}")
    final = None
    t1 = time.time()
    while time.time() - t1 < 40:
        code, final = h.http("GET", f"/jobs/{jid}")
        if code == 200 and final["status"] in TERMINAL:
            break
        time.sleep(1)
    effects = h.psql(f"SELECT attempt FROM effects WHERE job_id = '{jid}'")
    log(f"final status: {final and final['status']}, attempt {final and final['attempt']}, effects: {effects}")
    log("")
    if cancel_code != 202 or not final or final["status"] != "cancelled" or effects:
        log("RESULT: FAIL (accepted cancel did not end 'cancelled', or an effect exists)")
        sys.exit(1)
    log("RESULT: PASS")


if __name__ == "__main__":
    main()
