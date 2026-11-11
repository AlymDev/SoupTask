#!/usr/bin/env python3
"""Pagination test for GET /jobs (keyset cursor, newest first).

    python chaos/pagination_test.py
Part 1: 500 jobs, walk all pages (limit 37): no duplicates, no gaps, strictly newest-first.
Part 2: walk again WHILE new jobs are being inserted: no duplicates, and every job that existed
        before the walk started must be seen. Also the status filter and a bad cursor.
"""
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as h

N, LIMIT = 500, 37


def walk(status=None):
    seen, cursor, pages = [], None, 0
    while True:
        url = f"/jobs?limit={LIMIT}" + (f"&cursor={cursor}" if cursor else "") + (f"&status={status}" if status else "")
        code, body = h.http("GET", url)
        if code != 200:
            return seen, pages, f"HTTP {code} {body}"
        pages += 1
        seen += [(it["id"], it["created_at"]) for it in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            return seen, pages, None


def main():
    h.RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in h.RUNS.glob("pagination-*.txt") if (m := re.fullmatch(r"pagination-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    log = h.Tee(h.RUNS / f"pagination-{n:02d}.txt")
    log(f"PAGINATION TEST {n:02d} started {datetime.now().isoformat(timespec='seconds')}")
    if h.mutation_active():
        log(f"*** MUTATION ACTIVE: {h.mutation_active()} ***")
    h.psql("TRUNCATE effects, jobs;")
    run_id = uuid.uuid4().hex[:6]
    ids = []
    for i in range(N):
        code, j = h.http("POST", "/jobs", {"payload": {"duration_ms": 50}}, {"Idempotency-Key": f"pg-{run_id}-{i}"})
        if code != 201:
            sys.exit(f"create failed: {code} {j}")
        ids.append(j["id"])
    log(f"created {N} jobs")
    problems = []

    seen, pages, err = walk()
    log(f"part 1: {len(seen)} items in {pages} pages (expected {-(-N // LIMIT)} pages)")
    if err:
        problems.append(f"part 1 error: {err}")
    got = [s[0] for s in seen]
    if len(got) != len(set(got)):
        problems.append(f"part 1: {len(got) - len(set(got))} duplicate item(s)")
    if set(got) != set(ids):
        problems.append(f"part 1: missing {len(set(ids) - set(got))}, unknown {len(set(got) - set(ids))}")
    if pages != -(-N // LIMIT):
        problems.append(f"part 1: {pages} pages, expected {-(-N // LIMIT)}")
    order = [(s[1], s[0]) for s in seen]
    if order != sorted(order, reverse=True):
        problems.append("part 1: items are not strictly newest-first")

    stop = threading.Event()
    added = []

    def inserter():
        i = 0
        while not stop.is_set():
            code, j = h.http("POST", "/jobs", {"payload": {"duration_ms": 50}}, {"Idempotency-Key": f"pg2-{run_id}-{i}"})
            if code == 201:
                added.append(j["id"])
            i += 1
            time.sleep(0.01)

    th = threading.Thread(target=inserter, daemon=True)
    th.start()
    time.sleep(0.5)
    seen2, pages2, err2 = walk()
    stop.set()
    th.join(timeout=10)
    got2 = [s[0] for s in seen2]
    log(f"part 2 (inserts during the walk): {len(got2)} items in {pages2} pages, {len(added)} jobs inserted meanwhile")
    if err2:
        problems.append(f"part 2 error: {err2}")
    if len(got2) != len(set(got2)):
        problems.append(f"part 2: {len(got2) - len(set(got2))} duplicate item(s)")
    missing = set(ids) - set(got2)
    if missing:
        problems.append(f"part 2: {len(missing)} job(s) that existed before the walk were NOT returned")

    time.sleep(5)
    sseen, _, serr = walk(status="succeeded")
    cnt = int(h.psql("SELECT count(*) FROM jobs WHERE status='succeeded'")[0][0])
    log(f"status filter: walk(status=succeeded) returned {len(sseen)}, DB says {cnt} (jobs may still be finishing)")
    code, _ = h.http("GET", "/jobs?cursor=not-a-cursor")
    log(f"bad cursor -> HTTP {code} (expected 400)")
    if code != 400:
        problems.append(f"bad cursor returned {code}, expected 400")
    code, _ = h.http("GET", "/jobs?limit=0")
    log(f"limit=0 -> HTTP {code} (expected 422)")

    log("")
    if problems:
        log("RESULT: FAIL")
        for p in problems:
            log("  - " + p)
        sys.exit(1)
    log("RESULT: PASS (one run, ms-scale inserts; late-committing rows with an older created_at are the known untested case)")


if __name__ == "__main__":
    main()