"""Background memory upkeep: reflection runs off the run's critical path.

    run finishes ──enqueue("reflect", run_id)──►  runs/_upkeep/pending/  ──►  worker pool (opsagent upkeep --watch)
                                                                               └─ reflect() → lessons → memory + "lessons" event

Why: reflection is an LLM call that held every run for ~17.5 s after its verdict (docs/UPKEEP_AGENTS_TEST.md); the
requester doesn't need to wait for the agent's self-review. If no worker is alive, the runtime reflects inline, so
evals and demos behave the same with or without a worker.

Queue semantics (tested in experiments/upkeep_agents/):
  * enqueue is idempotent (deterministic job id), claim is an atomic rename (exactly one worker wins)
  * failures retry up to 3 attempts, then land in failed/; a crashed worker's claim is re-queued after 5 minutes
  * lesson ids are deterministic and ExperienceMemory.add skips known ids, so a retried job can't duplicate memory
"""

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from opsagent import config

HEARTBEAT_FRESH_S = 30


def upkeep_dir() -> Path:
    return config.RUNS_DIR / "_upkeep"


class FileQueue:
    def __init__(self, root: Path | None = None, max_attempts: int = 3):
        self.root = Path(root or upkeep_dir())
        self.max_attempts = max_attempts
        for d in ("pending", "claimed", "done", "failed"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(job_id: str) -> str:
        return job_id.replace("/", "_").replace(":", "__") + ".json"

    def enqueue(self, job_id: str, kind: str, payload: dict) -> bool:
        name = self._name(job_id)
        if any((self.root / d / name).exists() for d in ("pending", "claimed", "done")):
            return False
        tmp = self.root / "pending" / (name + ".tmp")
        tmp.write_text(json.dumps({"id": job_id, "kind": kind, "payload": payload, "attempts": 0, "errors": []}))
        tmp.replace(self.root / "pending" / name)
        return True

    def claim(self) -> tuple[Path, dict] | None:
        for f in sorted((self.root / "pending").glob("*.json")):
            target = self.root / "claimed" / f.name
            try:
                os.rename(f, target)
            except FileNotFoundError:
                continue  # another worker won
            target.touch()  # claim time, for stale detection
            return target, json.loads(target.read_text())
        return None

    def complete(self, claimed: Path, job: dict, result: dict) -> None:
        (self.root / "done" / claimed.name).write_text(json.dumps({**job, "result": result}))
        claimed.unlink()

    def fail(self, claimed: Path, job: dict, error: str) -> str:
        job["attempts"] += 1
        job["errors"].append(error[-500:])
        dest = "pending" if job["attempts"] < self.max_attempts else "failed"
        (self.root / dest / claimed.name).write_text(json.dumps(job))
        claimed.unlink()
        return dest

    def requeue_stale(self, older_than_s: float = 300) -> int:
        n = 0
        for f in (self.root / "claimed").glob("*.json"):
            if time.time() - f.stat().st_mtime > older_than_s:
                os.rename(f, self.root / "pending" / f.name)
                n += 1
        return n

    def counts(self) -> dict:
        return {d: len(list((self.root / d).glob("*.json"))) for d in ("pending", "claimed", "done", "failed")}

    # liveness: the runtime only hands work to the queue when a worker is actually running
    def beat(self) -> None:
        (self.root / "worker.heartbeat").write_text(str(time.time()))

    def worker_alive(self) -> bool:
        hb = self.root / "worker.heartbeat"
        try:
            return time.time() - float(hb.read_text()) < HEARTBEAT_FRESH_S
        except (FileNotFoundError, ValueError):
            return False


# ----------------------------------------------------------------------------- jobs

def reflect_job(payload: dict, attempt: int = 0) -> dict:
    """Rebuild what reflection needs from the run's own files, distil lessons, store them, and log them on the run."""
    from opsagent.contract import TaskContract
    from opsagent.llm import LLM
    from opsagent.memory import ExperienceMemory, reflect
    from opsagent.store import RunStore

    store = RunStore(payload["run_id"])
    cp = store.load_checkpoint() or {}
    contract = TaskContract.from_dict(cp["contract"])
    docs = "\n\n".join((config.KNOWLEDGE_DIR / f"{d}.md").read_text() for d in contract.relevant_docs
                       if (config.KNOWLEDGE_DIR / f"{d}.md").exists())
    llm = LLM()
    lessons = reflect(llm, store.run_id, contract.render(), "\n".join(cp.get("trace", [])), payload["verified"],
                      company_docs=docs)
    memory = ExperienceMemory(path=Path(payload["memory_path"]) if payload.get("memory_path") else None,
                              enabled=payload.get("memory_enabled", True))
    if memory.enabled:
        memory.add(lessons)
    store.log("lessons", lessons=[l.text for l in lessons], mode="async", attempt=attempt + 1,
              cost_usd=round(llm.usage.cost_usd, 4))
    return {"lessons": len(lessons), "cost_usd": round(llm.usage.cost_usd, 4)}


HANDLERS = {"reflect": reflect_job}


class WorkerPool:
    """N threads drain the queue (jobs are I/O-bound LLM calls). watch=True keeps polling and heartbeating."""

    def __init__(self, queue: FileQueue, workers: int = 4, handlers: dict | None = None, log=print):
        self.queue, self.workers, self.log = queue, workers, log
        self.handlers = handlers or HANDLERS
        self.stop = threading.Event()

    def _work_one(self, wid: int) -> bool:
        claimed = self.queue.claim()
        if claimed is None:
            return False
        path, job = claimed
        t0 = time.time()
        try:
            result = self.handlers[job["kind"]](job["payload"], attempt=job["attempts"])
            self.queue.complete(path, job, result)
            self.log(f"[w{wid}] done {job['id']} in {time.time() - t0:.1f}s {result}")
        except Exception as e:  # noqa: BLE001 — every failure becomes a retry or a failed job
            dest = self.queue.fail(path, job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}")
            self.log(f"[w{wid}] {job['id']} failed ({type(e).__name__}: {e}) -> {dest}")
        return True

    def _worker(self, wid: int, watch: bool) -> None:
        idle_since = None
        while not self.stop.is_set():
            if self._work_one(wid):
                idle_since = None
                continue
            if not watch and self.queue.counts()["claimed"] == 0:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > 0.5:
                    return
            time.sleep(0.25)

    def run(self, watch: bool = False) -> None:
        def heartbeat():
            while not self.stop.is_set():
                self.queue.beat()
                self.queue.requeue_stale()
                self.stop.wait(5)

        hb = threading.Thread(target=heartbeat, daemon=True)
        if watch:
            self.queue.beat()
            hb.start()
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            try:
                list(ex.map(lambda w: self._worker(w, watch), range(self.workers)))
            finally:
                self.stop.set()  # set before the executor waits for its threads (Ctrl-C in watch mode)
