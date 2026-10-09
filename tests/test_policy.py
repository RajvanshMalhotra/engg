from opsagent.policy import Policy, WriteRequest

U = "http://127.0.0.1:8799"


def test_reads_allowed_but_admin_denied(sandbox):
    p = Policy()
    assert p.evaluate(WriteRequest("GET", f"{U}/erp/bills")).effect == "allow"
    assert p.evaluate(WriteRequest("GET", f"{U}/__admin/state")).effect == "deny"


def test_large_payment_needs_approval_small_does_not(sandbox):
    p = Policy()
    big = p.evaluate(WriteRequest("POST", f"{U}/erp/bills/BILL-1003/schedule-payment", {"payment_date": "2026-10-20"}))
    assert (big.effect, big.rule_id) == ("require_approval", "large-payment")
    # BILL-1002 is 12,900 — below threshold, falls through to default allow
    small = p.evaluate(WriteRequest("POST", f"{U}/erp/bills/BILL-1002/schedule-payment", {"payment_date": "2026-10-15"}))
    assert small.effect == "allow"


def test_unknown_bill_fails_closed(sandbox):
    d = Policy().evaluate(WriteRequest("POST", f"{U}/erp/bills/BILL-9999/schedule-payment", {}))
    assert d.effect == "require_approval"


def test_external_email_needs_approval(sandbox):
    p = Policy()
    assert p.evaluate(WriteRequest("POST", f"{U}/mail/send", {"to": "priya.nair@kestrel.in"})).effect == "allow"
    assert p.evaluate(WriteRequest("POST", f"{U}/mail/send", {"to": "billing@acme-supplies.in"})).effect == "require_approval"
    mixed = p.evaluate(WriteRequest("POST", f"{U}/mail/send", {"to": "priya.nair@kestrel.in, x@evil.co"}))
    assert mixed.effect == "require_approval"


def test_segregation_of_duties_and_bank_changes(sandbox):
    p = Policy()
    assert p.evaluate(WriteRequest("POST", f"{U}/erp/vendors/V004/approve")).effect == "deny"
    assert p.evaluate(WriteRequest("POST", f"{U}/erp/vendors/V001/bank", {})).effect == "require_approval"


def test_read_only_actor_cannot_write(sandbox):
    d = Policy(read_only=True).evaluate(WriteRequest("POST", f"{U}/mail/send", {"to": "a@kestrel.in"}))
    assert (d.effect, d.rule_id) == ("deny", "read-only-actor")
