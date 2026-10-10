"""Browser tool against the live sandbox — no LLM involved."""

import re

import pytest

from opsagent.context import RunContext
from opsagent.human import ScriptedChannel
from opsagent.llm import LLM
from opsagent.policy import Policy
from opsagent.store import RunStore
from opsagent.tools.browser import Browser


def eid(text, pattern):
    return int(re.search(r"\[(\d+) " + pattern, text).group(1))


@pytest.fixture
def browser(sandbox):
    human = ScriptedChannel(approvals={"large-payment": False})
    ctx = RunContext(store=RunStore(), llm=LLM(), policy=Policy(), human=human)
    b = Browser(ctx)
    yield b
    b.close()


def login(b):
    r = b.open_url("/erp")
    b.fill_secret(eid(r.content, 'textbox "Username"'), "ERP_USER")
    r = b.fill_secret(eid(r.content, 'password "Password"'), "ERP_PASSWORD")
    assert "kestrel-sandbox-pass" not in r.content  # secrets never reach the model
    return b.click(eid(r.content, 'button "Sign in"'))


def test_observation_has_ids_and_untrusted_wrapper(browser):
    r = browser.open_url("/mail")
    assert r.ok and r.untrusted_source.startswith("browser:")
    assert re.search(r'\[\d+ link "GLX-7802 – scanned copy"\]', r.content)


def test_download_lands_in_workspace(browser):
    r = browser.open_url("/mail/m/3")
    d = browser.click(eid(r.content, 'link "INV-AC-2347.pdf" \\(download\\)'))
    assert d.ok and (browser.ctx.store.dir / "downloads" / "INV-AC-2347.pdf").exists()


def test_declined_approval_blocks_payment_and_nothing_changes(browser, sandbox):
    import httpx
    login(browser)
    r = browser.open_url("/erp/bills/BILL-1003")
    r = browser.type_text(eid(r.content, 'date "Payment date"'), "2026-10-20")
    res = browser.click(eid(r.content, 'button "Schedule payment"'))
    assert not res.ok and "DECLINED" in res.content
    bill = httpx.get(f"{sandbox}/erp/api/bills/BILL-1003", headers={"Authorization": "Bearer sandbox-readonly-token"}).json()
    assert bill["status"] == "Recorded"


def test_state_delta_and_http_error_surface(browser, sandbox):
    import httpx
    httpx.post(f"{sandbox}/__admin/faults", json={"faults": ["flaky_submit"]})
    login(browser)
    r = browser.open_url("/erp/bills/new")
    browser.select_option(eid(r.content, 'select "Vendor"'), "Acme Supplies Pvt Ltd")
    for label, val in [("textbox \"Invoice number\"", "T-1"), ("date \"Invoice date\"", "2026-09-30"),
                       ("date \"Due date\"", "2026-10-30"), ("textbox \"Amount", "100")]:
        r = browser.type_text(eid(browser.last_obs["text"], label), val)
    assert "Page changes" in r.content
    res = browser.click(eid(browser.last_obs["text"], 'button "Save bill"'))
    assert not res.ok and "HTTP 500" in res.content and "NOT saved" in res.content
    assert res.evidence  # screenshot taken after a write attempt


def test_navigation_outside_company_systems_blocked(browser):
    r = browser.open_url("https://example.com")
    assert not r.ok


def test_fill_form_grouped_action(browser):
    login(browser)
    r = browser.open_url("/erp/bills/new")
    t = r.content
    res = browser.fill_form([
        {"element": eid(t, 'select "Vendor"'), "value": "Globex"},
        {"element": eid(t, 'textbox "Invoice number"'), "value": "GLX-1"},
        {"element": eid(t, 'textbox "Amount'), "value": "18250.00"},
    ])
    assert res.ok and "fill 3 field(s)" in res.content
    assert 'selected="Globex Logistics LLP"' in res.content and 'value="18250.00"' in res.content


def test_granted_approval_is_visible_to_agent(sandbox):
    ctx = RunContext(store=RunStore(), llm=LLM(), policy=Policy(), human=ScriptedChannel(approvals={"large-payment": True}))
    b = Browser(ctx)
    try:
        login(b)
        r = b.open_url("/erp/bills/BILL-1003")
        r = b.type_text(eid(r.content, 'date "Payment date"'), "2026-10-20")
        res = b.click(eid(r.content, 'button "Schedule payment"'))
        assert res.ok and "APPROVED by eval-script" in res.content
    finally:
        b.close()


def test_read_only_context_shares_browser_and_cannot_write(sandbox):
    worker = RunContext(store=RunStore(), llm=LLM(), policy=Policy(), human=ScriptedChannel())
    wb = Browser(worker)
    try:
        login(wb)
        vctx = RunContext(store=worker.store, llm=worker.llm, policy=Policy(read_only=True), human=worker.human)
        vb = Browser(vctx, read_only=True, share=wb)
        r = vb.open_url("/mail?folder=sent")  # a second context in the same process works
        assert r.ok and "Sent" in r.content
        r = vb.open_url("/mail/compose")
        r = vb.click(eid(r.content, 'button "Send"'))
        assert not r.ok and "read-only" in r.content
        vb.close()
        assert wb.open_url("/erp").ok  # worker browser still alive after verifier closes its context
    finally:
        wb.close()


def test_long_field_values_are_not_silently_truncated(browser):
    r = browser.open_url("/mail/compose")
    body = "Vendor: Acme | Invoice: INV-AC-2347 | Amount: INR 55,224.00 | Bill ID: BILL-1004 | Payment date: 2026-10-30"
    r = browser.type_text(eid(r.content, 'textarea "Message"'), body)
    assert "BILL-1004 | Payment date: 2026-10-30" in r.content


def test_declined_action_is_never_reasked(sandbox):
    human = ScriptedChannel(approvals={"large-payment": False})
    ctx = RunContext(store=RunStore(), llm=LLM(), policy=Policy(), human=human)
    b = Browser(ctx)
    try:
        login(b)
        for attempt in range(2):
            r = b.open_url("/erp/bills/BILL-1003")
            r = b.type_text(eid(r.content, 'date "Payment date"'), f"2026-10-2{attempt}")  # different payload, same action
            res = b.click(eid(r.content, 'button "Schedule payment"'))
            assert not res.ok
        assert len([t for t in human.transcript if t["kind"] == "approval"]) == 1  # asked exactly once
        assert "already reviewed and DECLINED" in res.content
    finally:
        b.close()


def test_approved_multiline_external_email_is_actually_sent(sandbox):
    """Regression (held-out eval): browsers send textarea newlines as CRLF; the approved payload had LF."""
    import httpx
    human = ScriptedChannel(approvals={"external-email": True})
    ctx = RunContext(store=RunStore(), llm=LLM(), policy=Policy(), human=human)
    b = Browser(ctx)
    try:
        r = b.open_url("/mail/compose")
        r = b.fill_form([{"element": eid(r.content, 'textbox "To"'), "value": "billing@acme-supplies.in"},
                         {"element": eid(r.content, 'textbox "Subject"'), "value": "Re: INV-AC-2347"},
                         {"element": eid(r.content, 'textarea "Message"'), "value": "Hi Acme,\n\nLine two.\nThanks"}])
        res = b.click(eid(r.content, 'button "Send"'))
        assert res.ok and "APPROVED" in res.content and "NETWORK LAYER BLOCKED" not in res.content
        sent = httpx.get(f"{sandbox}/__admin/state").json()["sent"]
        assert any(m["recipients"] == "billing@acme-supplies.in" for m in sent)
    finally:
        b.close()
