"""Memory-upkeep micro-agents (prototype).

Reflector  — one job per finished run: rebuild the product's reflection inputs (contract + compact trace) from the
             run directory and call the product's own `opsagent.memory.reflect()` — same prompt, same rules.
Curator    — over the whole active lesson set: deterministic retirement first (a rule, no LLM), then concurrent
             LLM batches that propose merges/supersessions, then one cross-batch pass. Never deletes: every lesson
             keeps its id and gets a status; every change is logged with a reason.
"""

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from opsagent.contract import TaskContract
from opsagent.llm import LLM
from opsagent.memory import reflect

PASSED = ("verified", "justified_stop")


class BudgetStop(RuntimeError):
    pass


class Budget:
    """Shared across all workers: hard cap on total LLM spend for the experiment."""

    def __init__(self, cap_usd: float):
        self.cap, self.spent, self.calls = cap_usd, 0.0, 0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.spent >= self.cap:
                raise BudgetStop(f"experiment budget ${self.cap:.2f} reached (${self.spent:.3f})")

    def charge(self, llm: LLM) -> float:
        with self._lock:
            self.spent += llm.usage.cost_usd
            self.calls += llm.usage.calls
        return llm.usage.cost_usd


def lesson_id(run_id: str, text: str) -> str:
    """Deterministic: re-running a job (retry, duplicate enqueue) yields the same ids — no duplicate lessons."""
    return hashlib.sha1(f"{run_id}\n{text.strip().lower()}".encode()).hexdigest()[:10]


# ----------------------------------------------------------------------------- Reflector

def reflection_inputs(run_dir: Path) -> dict:
    events = []
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    status = next(e["status"] for e in reversed(events) if e["kind"] == "complete")
    cp_path = run_dir / "checkpoint.json"
    cp = json.loads(cp_path.read_text()) if cp_path.exists() else {}
    contract = cp.get("contract") or next(e["contract"] for e in events if e["kind"] == "contract")
    trace = cp.get("trace")
    if not trace:  # same format as Operator._execute builds
        trace = [f"{e['step']}. {e['tool']}({e['args'][:117]}) -> {'ok' if e['ok'] else 'FAILED'}: "
                 f"{(e.get('summary') or '')[:200]}" for e in events if e["kind"] == "tool"]
    inline = next((e for e in events if e["kind"] == "lessons"), None)
    verdict = next((e for e in reversed(events) if e["kind"] == "verdict"), None)
    return {"run_id": run_dir.name, "status": status, "verified": status in PASSED,
            "contract_text": TaskContract.from_dict(contract).render(), "trace": "\n".join(trace),
            "inline_lessons": inline["lessons"] if inline else [],
            "inline_reflect_gap_s": _gap(verdict["at"], inline["at"]) if (inline and verdict) else None}


def _gap(a: str, b: str) -> float:
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def make_reflector(budget: Budget, inject_failure_for: set[str] | None = None):
    def handle(payload: dict, attempt: int = 0) -> dict:
        run_dir = Path(payload["run_dir"])
        if inject_failure_for and run_dir.name in inject_failure_for and attempt == 0:
            raise RuntimeError("injected failure (first attempt)")
        budget.check()
        inp = reflection_inputs(run_dir)
        llm = LLM("deepseek")
        t0 = time.time()
        try:
            lessons = reflect(llm, inp["run_id"], inp["contract_text"], inp["trace"], inp["verified"])
        finally:
            cost = budget.charge(llm)
        out = []
        for l in lessons:
            d = asdict(l)
            d["id"] = lesson_id(inp["run_id"], l.text)
            d["status"] = "active"
            out.append(d)
        return {"run_id": inp["run_id"], "verified": inp["verified"], "lessons": out,
                "llm_seconds": round(time.time() - t0, 2), "cost_usd": round(cost, 5)}
    return handle


# ----------------------------------------------------------------------------- Curator

CURATE_SYSTEM = """You curate the lesson memory of an autonomous company operator. Lessons were distilled from past runs.
Given a list of lessons (id, kind, verified, run, text), propose ONLY these operations, as JSON:
{"merge": [{"ids": ["<id>", "<id>", ...], "text": "one canonical lesson", "kind": "workflow|pitfall|fact",
            "applies_to": ["keywords"], "reason": "why these say the same thing"}],
 "supersede": [{"id": "<older id>", "by": "<newer id>", "reason": "the direct contradiction"}]}
Rules:
- Merge only lessons that give the SAME guidance (paraphrases, or one strictly contains the other). Lessons about
  different steps, systems or situations are NOT duplicates even if they share words.
- The canonical text must keep every concrete detail from the merged lessons (pages, endpoints, field labels,
  thresholds, addresses). Never add facts that are not in the lessons. Two sentences at most.
- Supersede only when a newer lesson directly contradicts an older one; prefer the verified one.
- Each id may appear in at most one operation. If nothing qualifies, return empty lists."""


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9/_\-]{3,}", text.lower())}


def _batches(lessons: list[dict], size: int) -> list[list[dict]]:
    """Cheap pre-grouping so likely duplicates land in the same LLM batch: greedy by token overlap."""
    remaining = sorted(lessons, key=lambda l: (l["kind"], l["text"]))
    batches = []
    while remaining:
        seed = remaining.pop(0)
        st = _tokens(seed["text"])
        remaining.sort(key=lambda l: -len(st & _tokens(l["text"])) / (len(st | _tokens(l["text"])) or 1))
        batch, remaining = [seed] + remaining[: size - 1], remaining[size - 1:]
        batches.append(batch)
    return batches


def _ask(budget: Budget, lessons: list[dict]) -> dict:
    budget.check()
    llm = LLM("deepseek")
    listing = "\n".join(f"- id={l['id']} kind={l['kind']} verified={l['verified']} run={l['run_id']}: {l['text']}"
                        for l in lessons)
    try:
        return llm.json(CURATE_SYSTEM, f"LESSONS ({len(lessons)}):\n{listing}")
    finally:
        budget.charge(llm)


class Curator:
    def __init__(self, budget: Budget, batch_size: int = 18, workers: int = 4):
        self.budget, self.batch_size, self.workers = budget, batch_size, workers

    def curate(self, lessons: list[dict]) -> tuple[list[dict], list[dict], dict]:
        """Returns (all lessons with statuses incl. new canonical ones, operations log, stats)."""
        lessons = [dict(l) for l in lessons]
        by_id = {l["id"]: l for l in lessons}
        ops: list[dict] = []
        t0 = time.time()

        # 1. deterministic rule, no LLM: guidance from runs that did not pass verification is retired
        for l in lessons:
            if l["status"] == "active" and not l["verified"] and l["kind"] != "pitfall":
                l["status"] = "retired"
                ops.append({"op": "retire", "id": l["id"], "reason": "source run not verified (rule)"})

        # 2. concurrent LLM batches, then 3. one cross-batch pass over the survivors
        calls = 0
        for phase in ("batches", "cross"):
            active = [l for l in lessons if l["status"] == "active"]
            groups = _batches(active, self.batch_size) if phase == "batches" else [active]
            if phase == "cross" and len(_batches(active, self.batch_size)) <= 1 and calls:
                break  # everything already fitted in one batch
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                replies = list(ex.map(lambda g: (g, _ask(self.budget, g)), groups))
            calls += len(groups)
            for group, reply in replies:
                ops += self._apply(reply, {l["id"] for l in group}, by_id, lessons)
        stats = {"llm_calls": calls, "seconds": round(time.time() - t0, 2)}
        return lessons, ops, stats

    @staticmethod
    def _apply(reply: dict, allowed: set[str], by_id: dict, lessons: list[dict]) -> list[dict]:
        ops, used = [], set()
        for m in reply.get("merge", []) or []:
            ids = [i for i in dict.fromkeys(m.get("ids", [])) if i in allowed and i not in used
                   and by_id[i]["status"] == "active"]
            if len(ids) < 2 or not m.get("text"):
                continue
            members = [by_id[i] for i in ids]
            canon = {"id": "m-" + hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:8],
                     "at": max(x["at"] for x in members), "run_id": ",".join(sorted({x["run_id"] for x in members})),
                     "verified": all(x["verified"] for x in members), "kind": m.get("kind") or members[0]["kind"],
                     "applies_to": m.get("applies_to") or members[0]["applies_to"], "text": m["text"],
                     "status": "active", "merged_from": ids}
            for x in members:
                x["status"] = f"merged_into:{canon['id']}"
            if canon["id"] not in by_id:
                lessons.append(canon)
                by_id[canon["id"]] = canon
            used.update(ids)
            ops.append({"op": "merge", "ids": ids, "into": canon["id"], "text": m["text"], "reason": m.get("reason", "")})
        for s in reply.get("supersede", []) or []:
            old, new = s.get("id"), s.get("by")
            if old in allowed and new in by_id and old not in used and old != new and by_id[old]["status"] == "active":
                by_id[old]["status"] = f"superseded_by:{new}"
                used.add(old)
                ops.append({"op": "supersede", "id": old, "by": new, "reason": s.get("reason", "")})
        return ops
