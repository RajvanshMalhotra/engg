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
