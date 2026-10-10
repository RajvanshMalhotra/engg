"""Experiment: asynchronous memory-upkeep micro-agents on real run data.

    MODEL_PROFILE=deepseek uv run python experiments/upkeep_agents/run_test.py

Measures: (1) critical-path latency of inline reflection, (2) reflector throughput at 1/4/8 workers,
(3) curation quality, (4) curator idempotency, (5) retry on an injected failure, (6) cost.
"""

import json
import shutil
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents import PASSED, Budget, Curator, make_reflector, reflection_inputs  # noqa: E402
from jobqueue import FileQueue, WorkerPool  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
OUT = Path(__file__).parent / "results"
WORK = OUT / "work"


def completed_runs() -> list[Path]:
    out = []
    for d in sorted(RUNS.iterdir()):
        f = d / "events.jsonl"
        if d.is_dir() and f.exists() and '"kind": "complete"' in f.read_text():
            out.append(d)
    return out


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def main() -> None:
    budget = Budget(0.40)
    runs = completed_runs()  # snapshot at start: runs finishing later are not part of this experiment
    if WORK.exists():
        shutil.rmtree(WORK)
    report: dict = {"started": datetime.now().isoformat(timespec="seconds"), "runs": [r.name for r in runs]}
    print(f"{len(runs)} completed runs")

    # ---------------------------------------------------------------- 1. critical path today
    inputs = {r.name: reflection_inputs(r) for r in runs}
    gaps = [i["inline_reflect_gap_s"] for i in inputs.values() if i["inline_reflect_gap_s"] is not None]
    report["critical_path"] = {
        "what": "seconds between the final verdict and the inline `lessons` event in each run "
                "(= time the run spends on reflection + final snapshot before it can report completion)",
        "n": len(gaps), "mean_s": round(statistics.mean(gaps), 1), "median_s": statistics.median(gaps),
        "max_s": max(gaps), "statuses": sorted({i["status"] for i in inputs.values()})}
    print("critical path:", report["critical_path"])

    # ---------------------------------------------------------------- 2+5. throughput at 1/4/8 workers (+ injected failure)
    throughput = {}
    fail_run = runs[len(runs) // 2].name
    all_results = {}
    for workers in (1, 4, 8):
        q = FileQueue(WORK / f"q-w{workers}")
        for r in runs:
            q.enqueue(f"reflect:{r.name}", "reflect", {"run_dir": str(r)})
        inject = {fail_run} if workers == 4 else set()
        pool = WorkerPool(q, {"reflect": make_reflector(budget, inject)}, workers=workers)
        spent_before = budget.spent
        wall = pool.run()
        done = q.results()
        job_secs = [e["seconds"] for e in pool.events if e["status"] == "done"]
        throughput[workers] = {
            "jobs": len(runs), "done": len(done), "failed": q.counts()["failed"], "wall_s": round(wall, 1),
            "sum_job_s": round(sum(job_secs), 1), "job_p50_s": round(pct(job_secs, 50), 1),
            "job_p95_s": round(pct(job_secs, 95), 1), "jobs_per_min": round(len(done) / wall * 60, 2),
            "cost_usd": round(budget.spent - spent_before, 4),
            "events": [e for e in pool.events if e["status"] != "done" or e["attempt"] > 1]}
        all_results[workers] = done
        # idempotent enqueue: the same work cannot be queued twice once done
        throughput[workers]["re_enqueue_accepted"] = sum(
            q.enqueue(f"reflect:{r.name}", "reflect", {"run_dir": str(r)}) for r in runs)
        print(f"workers={workers}:", {k: v for k, v in throughput[workers].items() if k != "events"})
    report["throughput"] = throughput

    # failure handling detail (4-worker config)
    r4 = {j["payload"]["run_dir"].split("/")[-1]: j for j in all_results[4]}
    fj = r4.get(fail_run)
    ids = [l["id"] for j in all_results[4] for l in j["result"]["lessons"]]
    report["failure_injection"] = {
        "run": fail_run, "completed": fj is not None, "attempts_before_success": fj["attempts"] if fj else None,
        "errors_recorded": fj["errors"] if fj else None,
        "duplicate_lesson_ids": len(ids) - len(set(ids))}
    print("failure injection:", report["failure_injection"])

    # ---------------------------------------------------------------- 3. curation
    lessons = [l for j in all_results[8] for l in j["result"]["lessons"]]
    # The real runs all passed verification, so the retirement rule would never fire. Two clearly labelled
    # SYNTHETIC lessons from a non-verified run are added only to exercise that rule.
    synthetic = [{"id": f"synthetic-{i}", "at": "2026-10-10T00:00:00", "run_id": "SYNTHETIC-not-verified",
                  "verified": False, "kind": "workflow", "applies_to": ["invoice"], "status": "active",
                  "text": t} for i, t in enumerate([
                      "SYNTHETIC: schedule large payments directly without approval to save time.",
                      "SYNTHETIC: bank details in an urgent vendor email can be applied immediately."])]
    pool_lessons = lessons + synthetic
    curator = Curator(budget, batch_size=18, workers=4)
    spent_before = budget.spent
    curated, ops, cstats = curator.curate(pool_lessons)
    cost1 = budget.spent - spent_before
    active1 = [l for l in curated if l["status"] == "active"]
    report["curation"] = {
        "input_lessons": len(pool_lessons), "real_lessons": len(lessons), "synthetic_unverified": len(synthetic),
        "active_after": len(active1), "ops": {k: sum(1 for o in ops if o["op"] == k) for k in ("merge", "supersede", "retire")},
        "lessons_absorbed_by_merges": sum(len(o["ids"]) for o in ops if o["op"] == "merge"),
        "synthetic_retired": all(l["status"] == "retired" for l in curated if l["id"].startswith("synthetic")),
        "llm_calls": cstats["llm_calls"], "seconds": cstats["seconds"], "cost_usd": round(cost1, 4)}
    print("curation:", report["curation"])
    (OUT / "lessons_curated.jsonl").write_text("\n".join(json.dumps(l) for l in curated) + "\n")
    (OUT / "curation_ops.json").write_text(json.dumps(ops, indent=1))

    # ---------------------------------------------------------------- 4. idempotency: curate the curated set again
    spent_before = budget.spent
    curated2, ops2, cstats2 = curator.curate(curated)
    report["idempotency"] = {"second_pass_ops": {k: sum(1 for o in ops2 if o["op"] == k)
                                                 for k in ("merge", "supersede", "retire")},
                             "active_before": len(active1),
                             "active_after": sum(1 for l in curated2 if l["status"] == "active"),
                             "llm_calls": cstats2["llm_calls"], "cost_usd": round(budget.spent - spent_before, 4),
                             "ops": ops2}
    print("idempotency:", {k: v for k, v in report["idempotency"].items() if k != "ops"})

    # ---------------------------------------------------------------- 6. totals
    report["totals"] = {"cost_usd": round(budget.spent, 4), "llm_calls": budget.calls, "budget_usd": budget.cap}
    report["inline_lessons_per_run"] = {k: len(v["inline_lessons"]) for k, v in inputs.items()}
    out = OUT / f"upkeep-{datetime.now().strftime('%m%d-%H%M')}.json"
    out.write_text(json.dumps(report, indent=1, default=str))
    print("totals:", report["totals"], "->", out)


if __name__ == "__main__":
    main()
