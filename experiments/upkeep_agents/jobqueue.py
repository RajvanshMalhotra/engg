"""File-based job queue + worker pool for background memory upkeep (prototype).

Layout under <root>/:   pending/  claimed/  done/  failed/
  * enqueue: job JSON written to pending/ (id is deterministic, so re-enqueueing the same work is a no-op)
  * claim:   atomic os.rename pending/x -> claimed/x  (exactly one worker wins; losers get FileNotFoundError)
  * finish:  result written to done/x, claimed/x removed
  * failure: attempts += 1; back to pending/ until max_attempts, then failed/
Crash safety: a worker that dies leaves its job in claimed/; `requeue_stale()` moves old claims back to pending/.
"""

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class FileQueue:
    def __init__(self, root: Path, max_attempts: int = 3):
        self.root = Path(root)
        self.max_attempts = max_attempts
        for d in ("pending", "claimed", "done", "failed"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def _name(self, job_id: str) -> str:
        return job_id.replace("/", "_").replace(":", "__") + ".json"

    def enqueue(self, job_id: str, kind: str, payload: dict) -> bool:
        name = self._name(job_id)
        if any((self.root / d / name).exists() for d in ("pending", "claimed", "done")):
            return False  # idempotent enqueue
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

    def results(self) -> list[dict]:
        return [json.loads(f.read_text()) for f in sorted((self.root / "done").glob("*.json"))]


class WorkerPool:
    """N workers drain the queue concurrently. handlers: {kind: fn(payload) -> dict}. Each worker is a thread
    (jobs are I/O-bound LLM calls, so threads parallelise them well despite the GIL)."""

    def __init__(self, queue: FileQueue, handlers: dict, workers: int = 4, log=print):
        self.queue, self.handlers, self.workers, self.log = queue, handlers, workers, log
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _record(self, **e) -> None:
        with self._lock:
            self.events.append({"t": time.time(), **e})

    def _worker(self, wid: int) -> None:
        idle_since = None
        while True:
            claimed = self.queue.claim()
            if claimed is None:
                # retries are put back in pending/: wait briefly before deciding the queue is drained
                if self.queue.counts()["claimed"] == 0:
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > 0.5:
                        return
                time.sleep(0.05)
                continue
            idle_since = None
            path, job = claimed
            t0 = time.time()
            try:
                result = self.handlers[job["kind"]](job["payload"], attempt=job["attempts"])
                self.queue.complete(path, job, result)
                self._record(worker=wid, job=job["id"], status="done", seconds=time.time() - t0,
                             attempt=job["attempts"] + 1)
            except Exception as e:  # noqa: BLE001 — every failure becomes a retry or a failed job
                dest = self.queue.fail(path, job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}")
                self._record(worker=wid, job=job["id"], status=f"failed->{dest}", seconds=time.time() - t0,
                             attempt=job["attempts"], error=f"{type(e).__name__}: {e}")

    def run(self) -> float:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            list(ex.map(self._worker, range(self.workers)))
        return time.time() - t0
