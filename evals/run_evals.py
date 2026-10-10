"""Eval runner: tasks × conditions, scored against ground-truth sandbox state (not by an LLM).

    uv run python -m evals.run_evals                         # all tasks, clean condition
    uv run python -m evals.run_evals --tasks acme_invoice --conditions clean,faults --repeats 2
    uv run python -m evals.run_evals --memory-ablation acme_invoice   # run twice on fresh memory, compare

Per run we record:
  success          all checks pass (ground truth)
  subgoal score    fraction of checks passed (OSWorld-Pro style process signal)
  WES+ / WES-      OSWorld-Human efficiency: success × reference/actual steps, failure × −actual/max steps
  verifier agrees  did our LLM verifier's pass/fail match ground truth? (measures the verifier itself)
  steps, seconds, cost, approvals/questions asked
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from opsagent import config
from opsagent.human import ScriptedChannel
from opsagent.memory import ExperienceMemory
from opsagent.runtime import Operator

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"
SEED_BILLS = {"BILL-1001", "BILL-1002", "BILL-1003"}
SEED_VENDORS = {"V001", "V002", "V003"}


# ----------------------------------------------------------------------------- ground-truth checks

def _state() -> dict:
    return httpx.get(f"{config.SANDBOX_URL}/__admin/state", timeout=10).json()


def _bill(state, invoice_number):
    return [b for b in state["bills"] if b["invoice_number"] == invoice_number]


def check(name: str, args: dict, state: dict, before: dict) -> tuple[bool, str]:
    if name == "bill_exists":
        found = _bill(state, args["invoice_number"])
        if len(found) != 1:
            return False, f"{len(found)} bills for {args['invoice_number']}"
        b = found[0]
        wrong = [f"{k}={b[k]!r}≠{v!r}" for k, v in args.items() if k != "invoice_number" and
                 (abs(b[k] - v) > 0.01 if isinstance(v, float) else b[k] != v)]
        return not wrong, "ok" if not wrong else ", ".join(wrong)
    if name == "bill_absent":
        found = _bill(state, args["invoice_number"])
        return not found, f"{len(found)} bills"
    if name == "bill_count":
        n = len(_bill(state, args["invoice_number"]))
        return n == args["count"], f"{n} bills"
    if name == "bill_status":
        found = _bill(state, args["invoice_number"])
        if not found:
            return False, "no bill"
        b = found[-1]
        ok = b["status"] == args["status"] and ("payment_date" not in args or b["payment_date"] == args["payment_date"])
        return ok, f"status={b['status']} payment_date={b['payment_date']}"
    if name == "vendor_exists":
        found = [v for v in state["vendors"] if v["gstin"] == args["gstin"]]
        if len(found) != 1:
            return False, f"{len(found)} vendors with that GSTIN"
        wrong = [f"{k}={found[0][k]!r}" for k, v in args.items() if str(found[0][k]).strip() != v]
        return not wrong, "ok" if not wrong else ", ".join(wrong)
    if name == "vendor_status":
        found = [v for v in state["vendors"] if v["gstin"] == args["gstin"]]
        return bool(found) and found[0]["status"] == args["status"], found[0]["status"] if found else "missing"
    if name == "vendor_field":
        v = next((v for v in state["vendors"] if v["id"] == args["id"]), {})
        return v.get(args["field"]) == args["value"], f"{args['field']}={v.get(args['field'])!r}"
    if name == "sent_mail":
        for m in state["sent"]:
            if args["to"] not in m["recipients"]:
                continue
            text = (m["subject"] + " " + m["body"]).lower()
            groups = args.get("contains_groups", [])
            if all(c.lower() in text for c in args.get("contains", [])) and \
                    (not args.get("contains_any") or any(c.lower() in text for c in args["contains_any"])) and \
                    all(any(g.lower() in text for g in group) for group in groups):
                return True, f"sent: {m['subject']!r}"
        return False, f"no matching mail to {args['to']} ({len(state['sent'])} sent)"
    if name == "no_other_changes":
        problems = []
        allowed_bills = set(args.get("allow_new_bills", []))
        for b in state["bills"]:
            if b["id"] not in SEED_BILLS and b["invoice_number"] not in allowed_bills:
                problems.append(f"unexpected bill {b['id']} {b['invoice_number']}")
        old = {b["id"]: b for b in before["bills"]}
        for b in state["bills"]:
            if b["id"] in old and b != old[b["id"]] and b["invoice_number"] not in allowed_bills:
                problems.append(f"seed bill {b['id']} modified")
        allowed_vendors = set(args.get("allow_new_vendors", []))
        oldv = {v["id"]: v for v in before["vendors"]}
        for v in state["vendors"]:
            if v["id"] not in SEED_VENDORS and v["gstin"] not in allowed_vendors:
                problems.append(f"unexpected vendor {v['id']}")
            if v["id"] in oldv and v != oldv[v["id"]]:
                problems.append(f"vendor {v['id']} modified")
        allowed_ext = {a.lower() for a in args.get("allow_external", [])}
        external = [m["recipients"] for m in state["sent"] if not all(
            r.strip().endswith("@kestrel.in") or r.strip().lower() in allowed_ext
            for r in m["recipients"].replace(";", ",").split(",") if r.strip())]
        problems += [f"external email to {r}" for r in external]
        return not problems, "ok" if not problems else "; ".join(problems)
    if name == "summary_contains_any":
        events = [json.loads(l) for l in (config.RUNS_DIR / args["run_id"] / "events.jsonl").read_text().splitlines()]
        summary = " ".join((e.get("summary") or "") for e in events if e["kind"] == "complete").lower()
        hits = [g for g in args["groups"] if any(x.lower() in summary for x in g)]
        return len(hits) == len(args["groups"]), "ok" if len(hits) == len(args["groups"]) else f"summary missing {[g for g in args['groups'] if g not in hits]}"
    if name == "declined_not_reasked":
        events = [json.loads(l) for l in (config.RUNS_DIR / args["run_id"] / "events.jsonl").read_text().splitlines()]
        declined = [e["request"].split(" {", 1)[0] for e in events if e["kind"] == "approval" and not e["approved"]]
        repeats = {r for r in declined if declined.count(r) > 1}
        return not repeats, "ok" if not repeats else f"human asked again after declining: {sorted(repeats)}"
    raise ValueError(f"unknown check {name}")


# ----------------------------------------------------------------------------- running

def run_one(task: dict, condition: str, faults: list, memory_path: Path | None, max_steps: int, tag: str) -> dict:
    httpx.post(f"{config.SANDBOX_URL}/__admin/reset", timeout=30).raise_for_status()
    httpx.post(f"{config.SANDBOX_URL}/__admin/faults", json={"faults": faults}, timeout=10)
    before = _state()
    human = ScriptedChannel(approvals=task.get("human", {}).get("approvals", {}),
                            answers=[tuple(a) for a in task.get("human", {}).get("answers", [])])
    run_id = f"eval-{tag}-{task['id']}-{condition}"
    op = Operator(task["request"], human=human, run_id=run_id, max_steps=max_steps,
                  memory=memory_path is not None, memory_path=memory_path)
    t0 = time.time()
    result = op.run()
    seconds = time.time() - t0
    state = _state()
    checks = []
    for c in task["checks"]:
        needs_run = c["check"] in ("declined_not_reasked", "summary_contains_any")
        args = {**c.get("args", {}), "run_id": run_id} if needs_run else c.get("args", {})
        ok, detail = check(c["check"], args, state, before)
        checks.append({"name": c["name"], "ok": ok, "detail": detail})
    success = all(c["ok"] for c in checks)
    steps = max(result.steps, 1)
    verifier_pass = result.status in ("verified", "justified_stop")
    return {
        "task": task["id"], "condition": condition, "run_id": run_id, "status": result.status,
        "success": success, "subgoal_score": sum(c["ok"] for c in checks) / len(checks), "checks": checks,
        "steps": result.steps, "reference_steps": task["reference_steps"],
        "wes_plus": min(1.0, task["reference_steps"] / steps) if success else 0.0,
        "wes_minus": 0.0 if success else -(steps / max_steps),
        "verifier_agrees": verifier_pass == success, "verifier_status": result.status,
        "seconds": round(seconds, 1), "cost_usd": round(op.llm.usage.cost_usd, 4),
        "human": human.transcript,
    }


def _start_sandbox(port: int) -> subprocess.Popen:
    state = config.ROOT / "sandbox" / f"state_w{port}"
    env = {**os.environ, "SANDBOX_STATE_DIR": str(state)}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", str(port),
                             "--log-level", "warning"], env=env, cwd=config.ROOT)
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"sandbox on :{port} did not start")


def run_parallel(jobs: list[tuple], args, tag: str) -> list[dict]:
    """Each worker owns an isolated sandbox company (own port + database) and runs whole tasks in a subprocess,
    so runs never share state. Worker count is bounded by --parallel."""
    n = min(args.parallel, len(jobs))
    ports = [8780 + i for i in range(n)]
    sandboxes = [_start_sandbox(p) for p in ports]
    free = list(ports)
    rows: list[dict] = []

    def work(job):
        rep, cond, task = job
        port = free.pop()
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                out = tmp.name
            env = {**os.environ, "SANDBOX_URL": f"http://127.0.0.1:{port}"}
            log = open(RESULTS_DIR / f"worker-{tag}-r{rep}-{task}-{cond}.log", "w")
            subprocess.run([sys.executable, "-m", "evals.run_evals", "--tasks-file", args.tasks_file, "--tasks", task, "--conditions", cond,
                            "--max-steps", str(args.max_steps), "--tag", f"{tag}-r{rep}", "--out", out],
                           env=env, cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT)
            got = json.loads(Path(out).read_text()) if Path(out).stat().st_size else []
            for r in got:
                print(summarize([r]).splitlines()[2], flush=True)
            return got
        finally:
            free.append(port)

    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            for got in pool.map(work, jobs):
                rows += got
    finally:
        for s in sandboxes:
            s.terminate()
    return rows


def summarize(rows: list[dict]) -> str:
    lines = ["| Task | Condition | Success | Subgoals | Steps (ref) | WES+ | Verifier | Status | $ | s |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r['condition']} | {'✅' if r['success'] else '❌'} | {r['subgoal_score']:.0%} | "
                     f"{r['steps']} ({r['reference_steps']}) | {r['wes_plus']:.2f} | "
                     f"{'agrees' if r['verifier_agrees'] else 'DISAGREES'} | {r['status']} | {r['cost_usd']:.3f} | {r['seconds']:.0f} |")
    n = len(rows) or 1
    lines += ["", f"**Overall:** success {sum(r['success'] for r in rows)}/{len(rows)} · "
              f"mean subgoal {sum(r['subgoal_score'] for r in rows) / n:.0%} · "
              f"WES+ {sum(r['wes_plus'] for r in rows) / n:.2f} · WES− {sum(r['wes_minus'] for r in rows) / n:.2f} · "
              f"verifier agreement {sum(r['verifier_agrees'] for r in rows)}/{len(rows)} · "
              f"total ${sum(r['cost_usd'] for r in rows):.2f} · mean {sum(r['seconds'] for r in rows) / n:.0f}s/run"]
    for r in rows:
        failed = [c for c in r["checks"] if not c["ok"]]
        if failed:
            lines.append(f"- {r['task']}/{r['condition']}: " + "; ".join(f"{c['name']} → {c['detail']}" for c in failed))
    return "\n".join(lines)


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", default="", help="comma list of task ids (default all)")
    p.add_argument("--conditions", default="clean", help="comma list of: clean, faults")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--memory-ablation", default="", help="task id: run twice on a fresh memory file")
    p.add_argument("--rescore", default="", help="re-score a results JSON with the current reference_steps (no runs)")
    p.add_argument("--tasks-file", default="tasks.yaml", help="task set: tasks.yaml (dev) or tasks_holdout.yaml")
    p.add_argument("--parallel", type=int, default=1, help="run N tasks at once, each against its own sandbox")
    p.add_argument("--out", default="", help=argparse.SUPPRESS)   # worker mode: write rows here
    p.add_argument("--tag", default="", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    spec = yaml.safe_load((EVAL_DIR / args.tasks_file).read_text())
    tasks = {t["id"]: t for t in spec["tasks"]}
    if args.rescore:
        rows = json.loads(Path(args.rescore).read_text())
        for r in rows:
            r["reference_steps"] = tasks[r["task"]]["reference_steps"]
            r["wes_plus"] = min(1.0, r["reference_steps"] / max(r["steps"], 1)) if r["success"] else 0.0
        Path(args.rescore).write_text(json.dumps(rows, indent=1))
        Path(args.rescore).with_suffix(".md").write_text(summarize(rows))
        print(summarize(rows))
        return
    tag = args.tag or datetime.now().strftime("%m%d-%H%M")
    RESULTS_DIR.mkdir(exist_ok=True)
    rows = []
    if args.parallel > 1 and not args.memory_ablation:
        selected = [t for t in args.tasks.split(",") if t] or list(tasks)
        jobs = [(rep, cond, t) for rep in range(args.repeats) for cond in args.conditions.split(",") for t in selected]
        rows = run_parallel(jobs, args, tag)
        out = RESULTS_DIR / f"results-{tag}.json"
        out.write_text(json.dumps(rows, indent=1))
        (RESULTS_DIR / f"results-{tag}.md").write_text(summarize(rows))
        print("\n" + summarize(rows) + f"\n\nSaved {out}")
        return
    if args.memory_ablation:
        mem = RESULTS_DIR / f"memory-{tag}.jsonl"
        for i in (1, 2):
            rows.append(run_one(tasks[args.memory_ablation], f"memory-run{i}", [], mem, args.max_steps, f"{tag}-m{i}"))
            print(f"memory run {i}: {len(ExperienceMemory(path=mem).all())} lessons stored")
    else:
        selected = [tasks[t] for t in args.tasks.split(",") if t] or list(tasks.values())
        for rep in range(args.repeats):
            for cond in args.conditions.split(","):
                for task in selected:
                    rows.append(run_one(task, cond, spec["conditions"][cond], None, args.max_steps, f"{tag}-r{rep}"))
                    print(summarize(rows[-1:]).splitlines()[2])
    if args.out:  # worker mode: hand rows back to the parallel orchestrator
        Path(args.out).write_text(json.dumps(rows))
        return
    out = RESULTS_DIR / f"results-{tag}.json"
    out.write_text(json.dumps(rows, indent=1))
    md = summarize(rows)
    (RESULTS_DIR / f"results-{tag}.md").write_text(md)
    print("\n" + md + f"\n\nSaved {out}")


if __name__ == "__main__":
    main()
