import json
import time

from opsagent import config
from opsagent.memory import ExperienceMemory, Lesson
from opsagent.store import RunStore
from opsagent.upkeep import FileQueue, WorkerPool, reflect_job


def test_enqueue_idempotent_claim_and_retry(tmp_path):
    q = FileQueue(tmp_path / "q")
    assert q.enqueue("reflect:r1", "reflect", {"run_id": "r1"})
    assert not q.enqueue("reflect:r1", "reflect", {"run_id": "r1"})   # same work twice = no-op
    path, job = q.claim()
    assert q.claim() is None                                          # exactly one claimant
    assert q.fail(path, job, "boom") == "pending"                     # retried
    for _ in range(2):
        path, job = q.claim()
        dest = q.fail(path, job, "boom")
    assert dest == "failed" and q.counts()["failed"] == 1             # gives up after 3 attempts


def test_worker_pool_retries_then_completes(tmp_path):
    q = FileQueue(tmp_path / "q")
    calls = []

    def flaky(payload, attempt=0):
        calls.append(attempt)
        if attempt == 0:
            raise RuntimeError("transient")
        return {"ok": True}

    for i in range(3):
        q.enqueue(f"reflect:r{i}", "reflect", {"run_id": f"r{i}"})
    WorkerPool(q, workers=3, handlers={"reflect": flaky}, log=lambda *_: None).run(watch=False)
    assert q.counts() == {"pending": 0, "claimed": 0, "done": 3, "failed": 0}
    assert calls.count(0) == 3 and calls.count(1) == 3


def test_heartbeat_controls_async_mode(tmp_path):
    q = FileQueue(tmp_path / "q")
    assert not q.worker_alive()          # no worker → runtime reflects inline
    q.beat()
    assert q.worker_alive()              # worker running → runtime enqueues


def test_reflect_job_stores_lessons_and_logs_event(tmp_path, monkeypatch):
    store = RunStore("upkeep-test-run")
    store.checkpoint({"contract": {"request": "Process invoice", "interpretation": "x", "deliverable": "y",
                                   "constraints": [], "success_criteria": [], "subgoals": [],
                                   "relevant_docs": ["sop_invoice_processing"]},
                      "trace": ["1. open_url -> ok"]})
    seen = {}

    def fake_reflect(llm, run_id, contract_text, trace, verified, company_docs=""):
        seen["docs"] = company_docs
        return [Lesson("abc12345", "t", run_id, verified, "workflow", ["erp"], "Record bills at /erp/bills/new.")]

    monkeypatch.setattr("opsagent.memory.reflect", fake_reflect)
    mem_path = tmp_path / "lessons.jsonl"
    payload = {"run_id": store.run_id, "verified": True, "memory_enabled": True, "memory_path": str(mem_path)}
    assert reflect_job(payload)["lessons"] == 1
    reflect_job(payload, attempt=1)                       # a retry must not duplicate memory
    assert len(ExperienceMemory(path=mem_path).all()) == 1
    assert "Processing a vendor invoice" in seen["docs"]  # reflection is grounded in the SOP
    ev = [e for e in store.events() if e["kind"] == "lessons"]
    assert ev and ev[-1]["mode"] == "async"
