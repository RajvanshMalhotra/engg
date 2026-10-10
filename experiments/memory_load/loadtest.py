"""Load test for memory backends under concurrent reads + writes.

    uv run python loadtest.py                       # local: keyword, graph, two_tier (+ graph_uncapped no-load)
    QDRANT_URL=... python loadtest.py --backends vector,hybrid   # on the HPC (see hpc_hooks.py)

Per backend (each in its own fresh process, one at a time so latency isn't polluted by another backend):
  build time + RSS, quality (recall@5 vendor-scoped / precision@5 paraphrase) with no-load latency, then load stages
  with open-loop readers (latency measured from the *scheduled* send time, so queueing counts), paced writers, and a
  write->read visibility probe. The 10k-run sweep runs all backends in parallel (it measures quality/size, not latency).
"""

import argparse
import bisect
import json
import multiprocessing as mp
import os
import pickle
import platform
import random
import statistics
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import psutil

HERE = Path(__file__).parent
DATA = HERE / "data"
WORK = DATA / "work"
RESULTS = HERE / "results"
TIMEOUT_S = 1.0
STAGES = [(10, 5), (50, 20), (200, 20), (1000, 50)]


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p / 100 * len(xs)))] * 1000, 2)  # ms


def rss_mb():
    return round(psutil.Process().memory_info().rss / 2**20, 1)


def load_data(n):
    with open(DATA / f"company_{n}.pkl", "rb") as f:
        return pickle.load(f)


def make_backend(name, n):
    from backends import BACKENDS
    kw = {"workdir": str(WORK)}
    if name in ("vector", "hybrid"):
        import numpy as np
        vec_file = DATA / f"lessons_{n}.npy"
        if not vec_file.exists() or not os.getenv("QDRANT_URL"):
            return None  # needs precomputed lesson vectors + a running Qdrant server
        hooks = __import__(os.getenv("EMBED_HOOKS", "embedder"))   # embedder = local fastembed; hpc_hooks = remote
        kw.update(embeddings=np.load(vec_file), embed_queries=hooks.embed_queries,
                  embed_passages=hooks.embed_passages, collection=f"{name}_{n}")
    return BACKENDS[name](**kw)


def quality(backend, data, k=5):
    lat, vendor_r, topic_p = [], [], []
    for q in data["queries"]:
        t = time.perf_counter()
        res = backend.query(q["text"], k)
        lat.append(time.perf_counter() - t)
        truth = set(q["truth"])
        hits = len(truth & set(res))
        if q["kind"] == "vendor":
            vendor_r.append(hits / min(k, len(truth)))
        else:
            topic_p.append(hits / k)
    return {"vendor_recall@5": round(statistics.mean(vendor_r), 3), "paraphrase_precision@5": round(statistics.mean(topic_p), 3),
            "noload_p50_ms": pct(lat, 50), "noload_p95_ms": pct(lat, 95)}


class Writer:
    """Synthesises new runs like generate.py (Zipf vendor, ~10 facts incl. hubs, lesson with p=0.5)."""

    def __init__(self, data, seed=11):
        import generate as g
        self.g = g
        self.rnd = random.Random(seed)
        n_v = len(data["vendor_names"])
        w = [1 / (r + 1) ** 1.1 for r in range(n_v)]
        self.cum, acc = [], 0.0
        for x in w:
            acc += x
            self.cum.append(acc)
        self.total = acc
        self.names = data["vendor_names"]
        self.next_run = data["meta"]["n_runs"] + 1_000_000
        self.lock = threading.Lock()

    def make(self):
        with self.lock:
            r = self.next_run
            self.next_run += 1
            v = bisect.bisect_left(self.cum, self.rnd.random() * self.total)
            has_lesson = self.rnd.random() < 0.5
            topic = self.rnd.randrange(len(self.g.TOPICS))
            mention = self.rnd.random() < 0.5
            vendor_specific = self.rnd.random() < 0.3
            people = [self.rnd.choice(self.g.PEOPLE), self.rnd.choice(self.g.PEOPLE)]
            sop = self.rnd.choice(self.g.SOPS)
            lr = random.Random(r)
        run = f"run:{r}"
        ents = [f"vendor:{v}", f"bill:{r}", f"email:{r}", *people, *self.g.HUBS, sop, f"invoice:{r}"]
        lessons = []
        if has_lesson:
            name = self.names[v] if vendor_specific else None
            lessons = [{"id": f"W{r}", "text": self.g.lesson_text(topic, lr, name, mention and vendor_specific)}]
        return run, v, ents, lessons


def run_stage(backend, data, writer, reads_qps, writes_ps, duration):
    queries = [q["text"] for q in data["queries"]]
    rnd = random.Random(reads_qps * 31 + writes_ps)
    stop = threading.Event()
    read_lat, write_lat, lags, lag_fail = [], [], [], [0]
    pending = []
    pool = ThreadPoolExecutor(max_workers=64)

    def do_read(scheduled, text):
        backend.query(text, 5)
        read_lat.append(time.perf_counter() - scheduled)

    def reader():
        t0 = time.perf_counter()
        i = 0
        while not stop.is_set():
            due = t0 + i / reads_qps
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(min(delay, 0.05))
                continue
            pending.append(pool.submit(do_read, due, rnd.choice(queries)))
            i += 1

    def write_loop():
        t0 = time.perf_counter()
        i = 0
        while not stop.is_set():
            due = t0 + i / writes_ps
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(min(delay, 0.05))
                continue
            run, v, ents, lessons = writer.make()
            t = time.perf_counter()
            backend.add_run(run, v, ents, lessons)
            write_lat.append(time.perf_counter() - t)
            i += 1

    def lag_probe():
        import generate as g
        j = 0
        while not stop.is_set():
            j += 1
            vid = 50_000_000 + reads_qps * 1000 + j
            name = f"Probe Zeta Vendor {vid}"
            backend.link_vendor(name, vid)
            lid = f"P{vid}"
            lesson = {"id": lid, "text": g.lesson_text(j % len(g.TOPICS), random.Random(j), name, True)}
            t = time.perf_counter()
            backend.add_run(f"run:p{vid}", vid, [f"vendor:{vid}", *g.HUBS], [lesson])
            seen = False
            while time.perf_counter() - t < 3.0:
                if lid in backend.query(f"Process the latest invoice from {name}", 5):
                    seen = True
                    break
                time.sleep(0.005)
            if seen:
                lags.append(time.perf_counter() - t)
            else:
                lag_fail[0] += 1
            stop.wait(1.0)

    threads = [threading.Thread(target=f, daemon=True) for f in (reader, write_loop, lag_probe)]
    for th in threads:
        th.start()
    time.sleep(duration)
    stop.set()
    for th in threads:
        th.join(timeout=5)
    deadline = time.perf_counter() + 5
    for f in list(pending):
        left = deadline - time.perf_counter()
        if left <= 0:
            break
        try:
            f.result(timeout=left)
        except Exception:
            pass
    pool.shutdown(wait=False, cancel_futures=True)
    sent, done = len(pending), len(read_lat)
    timeouts = sum(1 for x in read_lat if x > TIMEOUT_S) + (sent - done)
    within = sum(1 for x in read_lat if x <= TIMEOUT_S)
    return {"target_reads_qps": reads_qps, "target_writes_ps": writes_ps,
            "achieved_reads_qps": round(within / duration, 1), "achieved_writes_ps": round(len(write_lat) / duration, 1),
            "read_p50_ms": pct(read_lat, 50), "read_p95_ms": pct(read_lat, 95), "read_p99_ms": pct(read_lat, 99),
            "timeouts": timeouts, "reads_sent": sent,
            "write_p50_ms": pct(write_lat, 50), "write_p95_ms": pct(write_lat, 95),
            "write_to_read_lag_p50_ms": pct(lags, 50), "write_to_read_lag_p95_ms": pct(lags, 95),
            "lag_probe_failures": lag_fail[0], "rss_mb": rss_mb()}


def run_backend(name, n, stages, duration, load=True):
    WORK.mkdir(parents=True, exist_ok=True)
    data = load_data(n)
    backend = make_backend(name, n)
    if backend is None:
        return {"backend": name, "n_runs": n, "status": "HPC run pending (needs Qdrant server + precomputed vectors)"}
    r0 = rss_mb()
    t = time.perf_counter()
    backend.build(data)
    build_s = round(time.perf_counter() - t, 2)
    out = {"backend": name, "n_runs": n, "build_s": build_s, "rss_build_mb": round(rss_mb() - r0, 1)}
    for q in data["queries"][:3]:   # warm-up (lazy model load, connection setup) excluded from timings — all backends
        backend.query(q["text"], 5)
    out.update(quality(backend, data))
    out["stages"] = []
    if load:
        writer = Writer(data)
        for reads, writes in stages:
            st = run_stage(backend, data, writer, reads, writes, duration)
            st["saturated"] = st["achieved_reads_qps"] < 0.7 * reads or (st["read_p95_ms"] or 0) > TIMEOUT_S * 1000
            out["stages"].append(st)
            print(f"  {name} r={reads} w={writes}: {st['achieved_reads_qps']} qps p95={st['read_p95_ms']}ms "
                  f"lag p50={st['write_to_read_lag_p50_ms']}ms timeouts={st['timeouts']}", flush=True)
            if st["saturated"]:
                break
    out["stats"] = backend.stats()
    return out


# ------------------------------------------------------------------------------------- multi-process FTS readers
def _mp_reader(args):
    path, queries, seconds, seed = args
    import sqlite3
    from backends import tokens
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rnd = random.Random(seed)
    lat, end = [], time.perf_counter() + seconds
    while time.perf_counter() < end:
        toks = tokens(rnd.choice(queries))
        match = " OR ".join(f'"{x}"' for x in toks)
        t = time.perf_counter()
        conn.execute("SELECT lid FROM lessons WHERE lessons MATCH ? ORDER BY bm25(lessons) LIMIT 5", (match,)).fetchall()
        lat.append(time.perf_counter() - t)
    return lat


def mp_fts(n, procs_list=(1, 4, 8), seconds=10):
    """Cold tier with N reader *processes* (no GIL) while one writer commits lessons at ~20/s."""
    from backends import TwoTier
    data = load_data(n)
    tt = TwoTier(workdir=str(WORK))
    tt.build(data)
    queries = [q["text"] for q in data["queries"]]
    writer = Writer(data, seed=99)
    results = []
    for procs in procs_list:
        stop = threading.Event()

        def write_loop():
            while not stop.is_set():
                run, v, ents, lessons = writer.make()
                tt.add_run(run, v, ents, lessons)
                time.sleep(0.05)

        th = threading.Thread(target=write_loop, daemon=True)
        th.start()
        with ProcessPoolExecutor(procs) as ex:
            lats = list(ex.map(_mp_reader, [(tt.cold.path, queries, seconds, i) for i in range(procs)]))
        stop.set()
        th.join()
        flat = [x for l in lats for x in l]
        results.append({"reader_processes": procs, "qps": round(len(flat) / seconds, 1), "p50_ms": pct(flat, 50),
                        "p95_ms": pct(flat, 95), "p99_ms": pct(flat, 99)})
        print(f"  fts multi-process x{procs}: {results[-1]}", flush=True)
    return results


def _sweep_one(args):
    name, n = args
    return run_backend(name, n, [], 0, load=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=100_000)
    p.add_argument("--backends", default="keyword,graph,two_tier,vector,hybrid")
    p.add_argument("--duration", type=float, default=15)
    p.add_argument("--skip-sweep", action="store_true")
    p.add_argument("--skip-load", action="store_true")
    p.add_argument("--sweep-backends", default=None, help="defaults to --backends + graph_uncapped")
    p.add_argument("--no-fts-mp", action="store_true")
    args = p.parse_args()
    RESULTS.mkdir(exist_ok=True)
    meta = {"machine": platform.platform(), "cpu": platform.processor() or platform.machine(),
            "cpu_count": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
            "python": platform.python_version(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(meta, flush=True)
    names = args.backends.split(",")
    ctx = mp.get_context("spawn")
    report = {"meta": meta, "scale_sweep": [], "load": [], "fts_multiprocess": None}

    if not args.skip_sweep:   # quality/size at both scales, no load; independent → run in parallel processes
        sweep_names = args.sweep_backends.split(",") if args.sweep_backends else names + ["graph_uncapped"]
        local = [nm for nm in sweep_names if nm not in ("vector", "hybrid")]
        jobs = [(nm, n) for n in (10_000, args.n) for nm in local]
        if jobs:
            with ProcessPoolExecutor(min(4, len(jobs)), mp_context=ctx, max_tasks_per_child=1) as ex:  # fresh process per job
                for res in ex.map(_sweep_one, jobs):
                    report["scale_sweep"].append(res)
                    print("sweep", json.dumps({k: v for k, v in res.items() if k != "stages"}), flush=True)
        for nm in [x for x in sweep_names if x in ("vector", "hybrid")]:   # these share one Qdrant server and embed
            for n in (10_000, args.n):                                       # model -> run sequentially, not in parallel
                with ProcessPoolExecutor(1, mp_context=ctx) as ex:
                    res = ex.submit(_sweep_one, (nm, n)).result()
                report["scale_sweep"].append(res)
                print("sweep", json.dumps({k: v for k, v in res.items() if k != "stages"}), flush=True)

    for nm in ([] if args.skip_load else names):   # load tests strictly one at a time, each in a fresh process
        print(f"load: {nm}", flush=True)
        with ProcessPoolExecutor(1, mp_context=ctx) as ex:
            res = ex.submit(run_backend, nm, args.n, STAGES, args.duration).result()
        report["load"].append(res)
        (RESULTS / f"load_{nm}_{args.n}.json").write_text(json.dumps(res, indent=1))

    if "two_tier" in names and not args.no_fts_mp:
        print("fts multi-process readers", flush=True)
        with ProcessPoolExecutor(1, mp_context=ctx) as ex:
            report["fts_multiprocess"] = ex.submit(mp_fts, args.n).result()

    out = RESULTS / f"report_{args.n}_{time.strftime('%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=1))
    print("saved", out, flush=True)


if __name__ == "__main__":
    main()
