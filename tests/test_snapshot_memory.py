from opsagent import snapshot
from opsagent.memory import ExperienceMemory, Lesson


def test_diff_reports_created_updated_deleted():
    before = {"erp/bills": [{"id": "B1", "amount": 10, "status": "Recorded"}, {"id": "B2", "amount": 5}]}
    after = {"erp/bills": [{"id": "B1", "amount": 10, "status": "Payment scheduled"}, {"id": "B3", "amount": 7}]}
    changes = snapshot.diff(before, after)
    assert any("CREATED B3" in c for c in changes)
    assert any("DELETED B2" in c for c in changes)
    assert any("UPDATED B1" in c and "Payment scheduled" in c for c in changes)


def test_snapshot_against_live_sandbox(sandbox):
    snap = snapshot.take()
    assert {r["id"] for r in snap["erp/bills"]} == {"BILL-1001", "BILL-1002", "BILL-1003"}
    assert snapshot.diff(snap, snapshot.take()) == []


def test_recall_prefers_relevant_and_verified(tmp_path):
    mem = ExperienceMemory(path=tmp_path / "l.jsonl")
    mem.add([
        Lesson("1", "t", "r1", True, "workflow", ["invoice", "erp"], "Record bills via ERP > Bills > Record a new bill."),
        Lesson("2", "t", "r2", False, "pitfall", ["invoice"], "ERP create-bill can 502 once; retry."),
        Lesson("3", "t", "r3", True, "fact", ["vendor", "onboarding"], "Vendor forms arrive as PDFs."),
    ])
    got = mem.recall("process the latest invoice from Acme", k=2)
    assert [l.id for l in got] == ["1", "2"]
    assert ExperienceMemory(path=tmp_path / "l.jsonl", enabled=False).recall("invoice") == []


def test_lesson_ids_are_deterministic_and_add_is_idempotent(tmp_path):
    from types import SimpleNamespace as NS
    from opsagent.memory import reflect

    class FakeLLM:
        def json(self, system, user):
            assert "COMPANY DOCUMENTS" in user and "never relax" not in user.lower()
            return {"lessons": [{"kind": "workflow", "applies_to": ["erp"], "text": "Record bills at /erp/bills/new."}]}

    a = reflect(FakeLLM(), "run-1", "c", "t", True, company_docs="SOP text")
    b = reflect(FakeLLM(), "run-1", "c", "t", True, company_docs="SOP text")
    assert a[0].id == b[0].id
    mem = ExperienceMemory(path=tmp_path / "l.jsonl")
    mem.add(a)
    mem.add(b)  # a retried reflection must not duplicate lessons
    assert len(mem.all()) == 1
