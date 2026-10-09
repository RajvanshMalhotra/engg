"""Deterministic action policy.

The LLM never decides whether a risky action is allowed. Every state-changing request — whether it
originates from a browser click or an API call — is evaluated here against company/policy.yaml.
Rules match on HTTP method + path, optionally refined by a named check that can look at the payload
or at live system state (e.g. the bill's amount in the ERP).
"""

import fnmatch
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from opsagent import config

READ_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class WriteRequest:
    method: str
    url: str
    fields: dict = field(default_factory=dict)

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    def describe(self) -> str:
        shown = {k: v for k, v in self.fields.items() if k not in ("password",)}
        return f"{self.method} {self.path} {shown}"


@dataclass
class Decision:
    effect: str  # allow | require_approval | deny
    rule_id: str
    reason: str = ""
    approver: str = ""


def _erp_bill(bill_id: str) -> dict | None:
    erp_api = next(a for a in config.load_systems()["erp"]["access"] if a["kind"] == "api")
    name, _, value = erp_api["auth_header"].partition(": ")
    resp = httpx.get(f"{erp_api['base_url']}/bills/{bill_id}", headers={name: value}, timeout=10)
    return resp.json() if resp.status_code == 200 else None


def _check(check: dict, req: WriteRequest) -> bool:
    kind, value = check["check"], check.get("value")
    if kind == "bill_amount_at_least":
        m = re.search(r"/bills/([^/]+)/", req.path)
        bill = _erp_bill(m.group(1)) if m else None
        # Fail closed: if we cannot establish the amount, treat it as large.
        return bill is None or float(bill["amount"]) >= float(value)
    if kind == "recipients_outside_domain":
        recipients = re.split(r"[,;\s]+", str(req.fields.get("to", "")).strip())
        return any(r and not r.lower().endswith("@" + value) for r in recipients)
    raise ValueError(f"unknown policy check {kind}")


class Policy:
    def __init__(self, rules: list[dict] | None = None, read_only: bool = False):
        self.rules = rules if rules is not None else config.load_policy()
        self.read_only = read_only  # the verifier runs with read_only=True: it can observe, never change

    def evaluate(self, req: WriteRequest) -> Decision:
        is_read = req.method.upper() in READ_METHODS
        if self.read_only and not is_read:
            return Decision("deny", "read-only-actor", "This actor is read-only (verification must not change state).")
        for rule in self.rules:
            match = rule.get("match", {})
            if "method" in match and match["method"].upper() != req.method.upper():
                continue
            if "path" in match and not fnmatch.fnmatch(req.path, match["path"]):
                continue
            if is_read and rule["effect"] != "deny":
                continue  # reads are only ever subject to explicit deny rules
            if "when" in rule and not _check(rule["when"], req):
                continue
            return Decision(rule["effect"], rule["id"], rule.get("reason", ""), rule.get("approver", ""))
        return Decision("allow", "default")
