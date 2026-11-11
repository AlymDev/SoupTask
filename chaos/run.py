#!/usr/bin/env python3
"""Chaos harness for the job queue. Standard library only.

Run from the repo root (the folder with docker-compose.yml), with `docker compose up` already running:

    python chaos/run.py --jobs 200          # short trial first
    python chaos/run.py                     # the real run: 10,000 jobs, 8 workers

What it does, all at the same time:
  * creates N jobs (16 threads); 20% of the keys are POSTed twice in parallel (insert race)
  * re-POSTs already-known keys all the time (same payload => must return the same job;
    5% use a different payload => must return 409)
  * cancels ~1000 jobs; half of them are picked from jobs that are RUNNING right now
  * kill -9 a random worker container about every 2 seconds (docker kill --signal KILL)
Then it waits until nothing is queued/running, stops the chaos and checks invariants with SQL.

Every run writes RUNS/chaos-NN.txt (+ worker logs). Failed runs are kept. Exit code 1 on any failure.
"""
import argparse
import json
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "RUNS"
BASE = "http://localhost:8000"
T0 = time.time()


class Tee:
    """print() to the console AND to the run file."""
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.lock = threading.Lock()

    def __call__(self, msg=""):
        with self.lock:
            print(msg, flush=True)
            self.f.write(msg + "\n")
            self.f.flush()


log = None  # set in main()


def ts():
    return f"[t={time.time() - T0:6.1f}s]"


def mutation_active():
    """chaos/mutate.py leaves app/.mutation behind while the worker is deliberately broken."""
    f = ROOT / "app" / ".mutation"
    return f.read_text().strip() if f.exists() else None


# ---------- helpers: HTTP, shell, SQL ----------
def http(method, path, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:
            return e.code, None
    except Exception as e:  # timeout, connection reset, ...
        return None, str(e)


def sh(cmd, input=None):
    p = subprocess.run(cmd, capture_output=True, text=True, input=input, cwd=ROOT,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def psql(sql):
    code, out, err = sh(["docker", "compose", "exec", "-T", "db", "psql", "-U", "soup", "-d", "soup",
                         "-q", "-At", "-F", "|", "-v", "ON_ERROR_STOP=1"], input=sql)
    if code != 0:
        raise RuntimeError(f"psql failed: {err}")
    return [line.split("|") for line in out.splitlines() if line]


# ---------- shared state ----------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()          # stops killer / dupers / canceller
        self.keys = {}                         # key -> {"i": n, "ids": set, "codes": []}
        self.key_list = []                     # confirmed keys, for dupers
        self.created_ids = []                  # confirmed job ids, for canceller
        self.errors = Counter()                # transport errors / unexpected codes
        self.dup_anomalies = []                # duplicate POST that did not behave
        self.dup_sent = 0
        self.cancels = []                      # dicts: id, code, status
        self.kills = []                        # dicts: t, name, how


st = State()


# ---------- chaos actors ----------
def create(run, i):
    key = f"chaos-{run}-{i}"
    for attempt in range(6):  # retry the SAME key on transport errors: that is what the key is for
        code, body = http("POST", "/jobs", {"payload": {"n": i}}, {"Idempotency-Key": key})
        if code in (200, 201) and isinstance(body, dict):
            with st.lock:
                d = st.keys.setdefault(key, {"i": i, "ids": set(), "codes": []})
                first = not d["ids"]
                d["ids"].add(body["id"])
                d["codes"].append(code)
                if first:
                    st.key_list.append(key)
                    st.created_ids.append(body["id"])
            return
        with st.lock:
            st.errors[f"create code={code}"] += 1
        time.sleep(0.2 * (attempt + 1))


def duper():
    """Re-POST known keys. Same payload => 200 + same id. Different payload => 409."""
    while not st.stop.is_set():
        with st.lock:
            if not st.key_list:
                key = None
            else:
                key = random.choice(st.key_list)
                d = st.keys[key]
                known = next(iter(d["ids"])) if len(d["ids"]) == 1 else None
                i = d["i"]
        if key is None:
            time.sleep(0.1)
            continue
        different = random.random() < 0.05
        code, body = http("POST", "/jobs", {"payload": {"n": -1 if different else i}},
                          {"Idempotency-Key": key})
        with st.lock:
            st.dup_sent += 1
            if code is None:
                st.errors["dup transport error"] += 1
            elif different:
                if code != 409:
                    st.dup_anomalies.append((key, f"different payload expected 409, got {code}"))
            else:
                got = body.get("id") if isinstance(body, dict) else None
                if code != 200 or (known is not None and got != known):
                    st.dup_anomalies.append((key, f"same payload expected 200+id {known}, got {code}+{got}"))
        time.sleep(0.05)


def canceller(total):
    sent = 0
    first_seen = {}      # (job id, attempt) -> local time we first saw it running
    stale_done = set()   # orphaned running jobs we already cancelled once
    while sent < total and not st.stop.is_set():
        job_id = None
        if random.random() < 0.5:  # aim at a RUNNING job: that is where cancel/complete races happen
            code, body = http("GET", "/jobs?status=running&limit=50")
            if code == 200 and body.get("items"):
                now = time.time()
                fresh, stale = [], []
                for it in body["items"]:
                    k = (it["id"], it["attempt"])
                    seen = first_seen.setdefault(k, now)
                    # a normal job runs < 1s. Still "running" after 1.5s => its worker is probably dead
                    # and it is waiting for the lease to expire. Cancelling those too eagerly turns every
                    # crash into a cancel and the retry path never runs (this happened in runs 01 and 03).
                    (fresh if now - seen < 1.5 else stale).append(k)
                stale = [k for k in stale if k not in stale_done]
                if fresh and not (stale and random.random() < 0.1):
                    job_id = random.choice(fresh)[0]
                elif stale:
                    k = random.choice(stale)
                    stale_done.add(k)
                    job_id = k[0]
        if job_id is None:
            with st.lock:
                job_id = random.choice(st.created_ids) if st.created_ids else None
        if job_id is None:
            time.sleep(0.2)
            continue
        code, body = http("POST", f"/jobs/{job_id}/cancel")
        with st.lock:
            if code in (200, 202) and isinstance(body, dict):
                st.cancels.append({"id": job_id, "code": code, "status": body["status"]})
            else:
                st.errors[f"cancel code={code}"] += 1
        sent += 1
        time.sleep(random.uniform(0.05, 0.5))


def inspect(cid):
    code, out, _ = sh(["docker", "inspect", "-f", "{{.Name}}|{{.State.Running}}|{{.State.Restarting}}", cid])
    name, running, restarting = (out.split("|") + ["", "", ""])[:3]
    return name.lstrip("/"), running == "true", restarting == "true"


def pick_victim(mode):
    """Returns (container id, how it was picked). 'busy' = a worker that currently holds a running job."""
    _, out, _ = sh(["docker", "compose", "ps", "-q", "worker"])
    ids = out.split()
    if not ids:
        return None, "none"
    if mode == "busy" or (mode == "mixed" and random.random() < 0.5):
        code, body = http("GET", "/jobs?status=running&limit=50")
        if code == 200 and body.get("items"):
            # worker_id starts with the container's hostname = first 12 chars of its container id
            prefixes = {it["worker_id"].split("-")[0] for it in body["items"] if it.get("worker_id")}
            busy = [i for i in ids if i[:12] in prefixes]
            if busy:
                return random.choice(busy), "busy"
    return random.choice(ids), "random"


def check_restart(cid, name, picked, t_kill):
    time.sleep(1.5)
    _, running, restarting = inspect(cid)
    if running or restarting:
        how = "docker restart policy"
    else:
        sh(["docker", "start", cid])
        how = "HARNESS had to `docker start` it (restart policy did not)"
    with st.lock:
        st.kills.append({"t": t_kill, "name": name, "picked": picked, "how": how})
    log(f"{ts()} KILL -9 {name} (picked: {picked}) -> restarted by {how}")


def killer(interval, mode):
    next_t = time.time() + interval
    while not st.stop.wait(max(0.0, next_t - time.time())):
        next_t += random.uniform(0.75 * interval, 1.25 * interval)  # schedule, so slow docker calls do not stretch the gap
        cid, picked = pick_victim(mode)
        if cid is None:
            log(f"{ts()} KILL skipped: no running worker")
            continue
        name = inspect(cid)[0]
        t_kill = time.time() - T0
        sh(["docker", "kill", "--signal", "KILL", cid])
        threading.Thread(target=check_restart, args=(cid, name, picked, t_kill), daemon=True).start()


# ---------- invariant checks ----------
def check_sql(results, name, desc, sql):
    rows = psql(sql)
    ok = not rows
    log(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(rows)} violation(s). {desc}")
    for r in rows[:10]:
        log("        " + " | ".join(r))
    results.append((name, ok))


def check_py(results, name, desc, violations):
    ok = not violations
    log(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(violations)} violation(s). {desc}")
    for v in violations[:10]:
        log(f"        {v}")
    results.append((name, ok))


def run_checks(args):
    results = []
    log("")
    log("=== INVARIANTS ===")
    db = {r[0]: (r[1], r[2]) for r in psql("SELECT idempotency_key, id, status FROM jobs")}
    final = {v[0]: v[1] for v in db.values()}

    # 1. no job lost
    lost = []
    for key, d in st.keys.items():
        if key not in db:
            lost.append(f"key {key} was confirmed (HTTP 2xx) but is not in the database")
        elif db[key][0] not in d["ids"]:
            lost.append(f"key {key}: id in DB {db[key][0]} differs from id returned to client {d['ids']}")
    check_py(results, "no_job_lost", "every key the API confirmed exists in the DB with the same id", lost)
    unexpected = [k for k in db if k not in st.keys]
    log(f"[INFO] rows in DB the harness never got a 2xx for: {len(unexpected)} "
        f"(possible only if every retry of a create timed out)")
    count_ok = len(db) == len(st.keys)
    log(f"[{'PASS' if count_ok else 'FAIL'}] row_count: DB has {len(db)} jobs, harness confirmed {len(st.keys)}")
    results.append(("row_count", count_ok))

    # 2. idempotency from the client's point of view
    idem = []
    for key, d in st.keys.items():
        if len(d["ids"]) != 1:
            idem.append(f"{key}: got {len(d['ids'])} different ids for one key: {d['ids']}")
        if d["codes"].count(201) > 1:
            idem.append(f"{key}: {d['codes'].count(201)} responses said 'created' (201)")
    check_py(results, "idempotent_create",
             "one key => one id, and at most one 201 (includes the parallel-duplicate keys)", idem)
    check_py(results, "duplicate_posts_behave",
             f"{st.dup_sent} repeated POSTs: same payload => 200 + same id, other payload => 409",
             [f"{k}: {m}" for k, m in st.dup_anomalies])
    check_py(results, "no_http_errors",
             "no transport errors or unexpected HTTP codes (500s count as failures; availability matters)",
             [f"{k}: {v}" for k, v in st.errors.items()])

    # 3. effects
    check_sql(results, "no_double_effect", "no job has more than one effect row",
              "SELECT job_id, count(*) FROM effects GROUP BY job_id HAVING count(*) > 1")
    check_sql(results, "none_stuck", "after settling nothing is queued or running",
              "SELECT id, status, attempt, worker_id FROM jobs WHERE status IN ('queued','running')")
    check_sql(results, "succeeded_has_effect", "every succeeded job has exactly one effect",
              "SELECT j.id FROM jobs j WHERE j.status='succeeded' AND "
              "(SELECT count(*) FROM effects e WHERE e.job_id=j.id) <> 1")
    check_sql(results, "effect_only_if_succeeded", "cancelled / failed jobs have no effect",
              "SELECT j.id, j.status FROM effects e JOIN jobs j ON j.id=e.job_id WHERE j.status <> 'succeeded'")
    check_sql(results, "effect_from_final_attempt", "the effect was written by the job's last attempt (fencing)",
              "SELECT j.id, j.attempt, e.attempt FROM effects e JOIN jobs j ON j.id=e.job_id WHERE e.attempt <> j.attempt")
    check_sql(results, "failed_only_if_exhausted", f"no job fails unless it used all {args.max_attempts} attempts",
              f"SELECT id, attempt FROM jobs WHERE status='failed' AND attempt < {args.max_attempts}")
    check_sql(results, "terminal_has_no_lease", "finished jobs hold no lease",
              "SELECT id, status FROM jobs WHERE status IN ('succeeded','failed','cancelled') AND lease_until IS NOT NULL")

    # 4. cancel was honoured
    bad = []
    for c in st.cancels:
        f = final.get(c["id"])
        if (c["code"] == 202 or c["status"] == "cancelled") and f != "cancelled":
            bad.append(f"{c['id']}: API said cancel accepted (HTTP {c['code']}, {c['status']}) but final status is {f}")
        elif c["status"] in ("succeeded", "failed") and f != c["status"]:
            bad.append(f"{c['id']}: API said already {c['status']} but final status is {f}")
    check_py(results, "cancel_is_honoured",
             "accepted cancels end 'cancelled'; terminal jobs never change afterwards", bad)

    if args.no_kill:
        # With no kills and 0.05-0.5s jobs, nothing can make a lease expire, so attempt > 1 can only
        # mean two workers claimed the same job (a double claim).
        check_sql(results, "no_double_claim_without_kills",
                  "in a run without kills no job may be claimed more than once",
                  "SELECT id, attempt, status FROM jobs WHERE attempt > 1")
    return results


def report_stats(args, timed_out):
    log("")
    log("=== STATS (to judge whether the chaos actually did anything) ===")
    status = psql("SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status")
    log("final status counts : " + ", ".join(f"{s}={n}" for s, n in status))
    att = psql("SELECT attempt, count(*) FROM jobs GROUP BY attempt ORDER BY attempt")
    log("attempts per job    : " + ", ".join(f"{a}x={n}" for a, n in att))
    retried = sum(int(n) for a, n in att if int(a) > 1)
    log(f"jobs claimed more than once (killed/expired and retried): {retried}")
    ks = sorted(st.kills, key=lambda k: k["t"])
    gaps = [b["t"] - a["t"] for a, b in zip(ks, ks[1:])]
    log(f"kills performed      : {len(ks)}  (picked busy worker={sum(k['picked'] == 'busy' for k in ks)}, "
        f"random={sum(k['picked'] == 'random' for k in ks)}; "
        f"restarted by harness instead of docker: {sum('HARNESS' in k['how'] for k in ks)})")
    if gaps:
        log(f"average gap between kills: {sum(gaps) / len(gaps):.1f}s (wanted about {args.kill_interval}s)")
    cc = Counter((c["code"], c["status"]) for c in st.cancels)
    log("cancel responses    : " + ", ".join(f"HTTP {k[0]}/{k[1]}={v}" for k, v in sorted(cc.items())))
    log(f"repeated POSTs sent  : {st.dup_sent}")
    log(f"transport/unexpected errors: {dict(st.errors) or 'none'}")
    log(f"timed out waiting to settle: {timed_out}")
    if not args.no_kill and not st.kills:
        log("WARNING: no worker was killed. This run proves nothing about crashes.")
    if not args.no_kill and retried == 0:
        log("WARNING: no job was ever retried. The chaos had no effect on any job; the invariants above are vacuous.")


def save_worker_logs(path, since):
    """Only log lines since the start of this run (earlier runs' lines used to leak in)."""
    _, out, _ = sh(["docker", "compose", "logs", "--no-color", "--since", since, "worker"])
    Path(path).write_text(out, encoding="utf-8")
    log(f"worker logs since {since} saved to {Path(path).name}: "
        f"ZOMBIE rejections={out.count('ZOMBIE write rejected')}, lost lease={out.count('lost lease')}, "
        f"reaper actions (requeue OR cancel-finish OR fail)={out.count('reaper handled')}, "
        f"worker loop errors={out.count('worker loop error')}, worker (re)starts={out.count('starting (lease')}")
    return out.count("ZOMBIE write rejected")


def main():
    global log, BASE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=10000)
    ap.add_argument("--workers", type=int, default=8, help="expected number of worker containers")
    ap.add_argument("--kill-interval", type=float, default=2.0, help="seconds between kill -9 (+-25%%)")
    ap.add_argument("--kill-mode", choices=["random", "busy", "mixed"], default="mixed",
                    help="random = any worker; busy = only workers holding a running job; mixed = 50/50")
    ap.add_argument("--cancels", type=int, default=1000)
    ap.add_argument("--dup-threads", type=int, default=2)
    ap.add_argument("--producers", type=int, default=16)
    ap.add_argument("--parallel-dup-fraction", type=float, default=0.2)
    ap.add_argument("--max-attempts", type=int, default=5, help="must equal MAX_ATTEMPTS in docker-compose.yml")
    ap.add_argument("--timeout", type=int, default=1200, help="max seconds to wait for all jobs to finish")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--no-kill", action="store_true", help="control run without kill -9")
    ap.add_argument("--allow-worker-mismatch", action="store_true",
                    help="run even if the number of running worker containers is not --workers")
    args = ap.parse_args()
    BASE = args.base_url

    RUNS.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in RUNS.glob("chaos-*.txt") if (m := re.fullmatch(r"chaos-(\d+)\.txt", p.name))]
    n = max(nums, default=0) + 1
    run_file = RUNS / f"chaos-{n:02d}.txt"
    log = Tee(run_file)
    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    random.seed(seed)
    run = f"r{n:02d}"

    log(f"CHAOS RUN {n:02d}  started {datetime.now().isoformat(timespec='seconds')}")
    log(f"args: {vars(args)}  seed={seed}")
    if mutation_active():
        log(f"*** MUTATION ACTIVE: {mutation_active()}  (deliberately broken worker; a FAIL is the expected result) ***")
    log(f"python {sys.version.split()[0]}; git commit: {sh(['git', 'rev-parse', '--short', 'HEAD'])[1] or 'n/a'}")
    log(sh(["docker", "compose", "ps"])[1])

    # wait for the API
    for _ in range(60):
        if http("GET", "/healthz")[0] == 200:
            break
        time.sleep(1)
    else:
        log("API did not become healthy. Is `docker compose up` running?")
        sys.exit(2)

    n_workers = len(sh(["docker", "compose", "ps", "-q", "worker"])[1].split())
    log(f"running worker containers: {n_workers} (expected {args.workers})")
    if n_workers != args.workers:
        msg = f"{n_workers} worker containers are running, expected {args.workers}"
        if not args.allow_worker_mismatch:
            log(f"ERROR: {msg}. Run `docker compose up -d` to start the stopped ones, "
                f"or pass --allow-worker-mismatch. Aborting so the run is not mislabelled.")
            sys.exit(2)
        log(f"WARNING: {msg} (continuing because of --allow-worker-mismatch)")

    log("resetting tables (TRUNCATE effects, jobs)")
    psql("TRUNCATE effects, jobs;")
    # start time taken from the DB clock (same machine as the docker daemon), not from this PC's clock
    since = psql("""SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")[0][0]

    # ---- start the chaos ----
    threads = []
    if not args.no_kill:
        threads.append(threading.Thread(target=killer, args=(args.kill_interval, args.kill_mode), daemon=True))
    threads += [threading.Thread(target=duper, daemon=True) for _ in range(args.dup_threads)]
    threads.append(threading.Thread(target=canceller, args=(args.cancels,), daemon=True))
    for t in threads:
        t.start()

    dup_idx = set(random.sample(range(args.jobs), int(args.jobs * args.parallel_dup_fraction)))
    tasks = []
    for i in range(args.jobs):
        tasks.append(i)
        if i in dup_idx:
            tasks.append(i)  # same key submitted twice back to back => parallel POSTs race on INSERT
    pool = ThreadPoolExecutor(max_workers=args.producers)
    futures = [pool.submit(create, run, i) for i in tasks]

    # ---- wait until settled ----
    timed_out = False
    while True:
        time.sleep(5)
        producers_done = all(f.done() for f in futures)
        counts = {s: int(c) for s, c in psql("SELECT status, count(*) FROM jobs GROUP BY status")}
        log(f"{ts()} created={len(st.keys)}/{args.jobs} " +
            " ".join(f"{s}={counts.get(s, 0)}" for s in ("queued", "running", "succeeded", "cancelled", "failed")) +
            f" kills={len(st.kills)} cancels={len(st.cancels)} dups={st.dup_sent}")
        if producers_done and counts.get("queued", 0) + counts.get("running", 0) == 0:
            break
        if time.time() - T0 > args.timeout:
            log(f"{ts()} TIMEOUT: jobs still queued/running after {args.timeout}s")
            timed_out = True
            break

    st.stop.set()
    for t in threads:
        t.join(timeout=20)
    pool.shutdown(wait=True)
    time.sleep(3)
    log(f"{ts()} chaos stopped, checking invariants")

    results = run_checks(args)
    report_stats(args, timed_out)
    zombies = save_worker_logs(RUNS / f"chaos-{n:02d}-worker-logs.txt", since)
    fenced_ok = zombies == 0
    log(f"[{'PASS' if fenced_ok else 'FAIL'}] no_fenced_writes: {zombies} stale write(s) rejected by the attempt check. "
        f"This run freezes no worker, so a rejection means a double claim (or a lease that expired on a live worker).")
    if not fenced_ok:
        results.append(("no_fenced_writes", False))

    failed = [name for name, ok in results if not ok]
    if timed_out:
        failed.append("settled_before_timeout")
    log("")
    if failed:
        log(f"RESULT: FAIL ({', '.join(failed)})")
        log("Do not edit or delete this file. Describe what happened in DESIGN.md section 10.")
        sys.exit(1)
    log(f"RESULT: PASS ({len(results)} invariants held in this run). "
        f"A pass in one run is not a proof; see 'what the harness does not catch'.")


if __name__ == "__main__":
    main()