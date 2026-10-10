"""Synthetic 'large company' for the memory load test (seeded, deterministic).

Produces runs, vendors, facts (run -> entity edges, including hub nodes every run touches), lessons with
provenance, and a held-out planted query set with ground truth. Generation is sharded across processes.
"""

import json
import pickle
import random
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

OUT = Path(__file__).parent / "data"

# ----------------------------------------------------------------------------------------------- vocabulary
ADJ = ["Blue", "Iron", "Silver", "North", "Bright", "Cedar", "Summit", "Harbor", "Falcon", "Granite", "Coral", "Maple",
       "Orion", "Pioneer", "Quartz", "Rapid", "Sterling", "Tidal", "Union", "Vertex", "Willow", "Zenith", "Amber",
       "Beacon", "Crest", "Delta", "Evergreen", "Frontier", "Golden", "Horizon", "Ivory", "Jade", "Keystone", "Lumen"]
NOUN = ["Harbor", "Peak", "Field", "Ridge", "Valley", "Bridge", "Forge", "Stone", "River", "Grove", "Point", "Gate",
        "Works", "Labs", "Systems", "Line", "Path", "Wave", "Hill", "Mills", "Port", "Yard", "Craft", "Source"]
SUFFIX = ["Logistics", "Supplies", "Traders", "Industries", "Packaging", "Electricals", "Chemicals", "Textiles",
          "Software", "Furniture", "Foods", "Metals", "Plastics", "Printing", "Hardware", "Services", "Pharma",
          "Engineering", "Media", "Freight"]

# 40 topics: synonym groups + outcome phrases. Lessons and queries are built from DIFFERENT frames and independently
# sampled synonyms, so lexical overlap between a query and its relevant lessons is partial (semantic retrieval matters).
T = lambda name, a, b, c, acts: (name, [a, b, c], acts)  # noqa: E731
TOPICS = [
    T("bank_change", ["bank details", "bank account", "account number", "banking information"], ["changed", "updated", "replaced", "switched"], ["vendor", "supplier", "payee"], ["needs Finance Manager approval and a call-back", "must never be applied from an email alone", "is a common fraud pattern"]),
    T("lookalike_domain", ["look-alike domain", "spoofed sender", "misspelled domain", "fake sender address"], ["email", "message", "mail"], ["invoice", "payment request", "bill"], ["must be reported to the AP lead as suspected fraud", "should not be processed", "is phishing"]),
    T("duplicate_invoice", ["duplicate invoice", "invoice already recorded", "repeated bill", "same invoice number twice"], ["check", "search", "look up"], ["ERP", "ledger", "accounts system"], ["before recording anything", "to avoid paying twice", "and report the existing bill id"]),
    T("large_payment", ["large payment", "payment above 50k", "high-value payment", "big transfer"], ["schedule", "release", "approve"], ["bill", "invoice", "payable"], ["requires Finance Manager sign-off first", "is routed for approval automatically", "cannot be scheduled without approval"]),
    T("scanned_invoice", ["scanned invoice", "photo of an invoice", "image attachment", "phone scan"], ["read", "extract", "transcribe"], ["amount", "totals", "due date"], ["with the vision model", "and double-check digits", "since there is no text layer"]),
    T("session_expiry", ["session expired", "logged out", "login timeout", "signed out mid-task"], ["ERP", "finance system", "accounts portal"], ["page", "form", "screen"], ["sign in again with stored credentials", "re-open the page after login", "is transient, retry after re-login"]),
    T("upstream_timeout", ["502 error", "upstream timeout", "server error", "save failed"], ["bill", "record", "entry"], ["ERP", "ledger", "system"], ["retry once, it is usually transient", "check it was not saved before retrying", "the record was not created"]),
    T("ui_relabel", ["menu renamed", "fields relabelled", "layout changed", "labels moved"], ["Bills", "Payables", "Suppliers"], ["page", "form", "screen"], ["read labels semantically, not by position", "Payables is the same as Bills", "Suppliers means vendors"]),
    T("vendor_onboarding", ["new vendor", "onboard a supplier", "register a vendor", "add a supplier"], ["registration form", "vendor form", "signup PDF"], ["GSTIN", "IFSC", "bank account"], ["create it as Pending verification", "never approve it yourself", "notify procurement with the vendor id"]),
    T("segregation", ["approve vendor", "self-approval", "vendor approval", "approving suppliers"], ["AP", "operator", "clerk"], ["policy", "control", "rule"], ["is reserved for the Finance Manager", "is blocked by segregation of duties", "must not be done by automation"]),
    T("ap_confirmation", ["confirmation email", "notify AP lead", "processing confirmation", "summary email"], ["bill id", "amount", "payment date"], ["Priya", "AP lead", "accounts payable lead"], ["include vendor, invoice number, amount and bill id", "send after scheduling", "keep it internal"]),
    T("external_email", ["external email", "email to vendor", "outside recipient", "mail outside kestrel"], ["send", "reply", "forward"], ["vendor", "supplier", "third party"], ["needs human approval first", "is not sent automatically", "requires AP lead approval"]),
    T("due_date", ["due date", "payment date", "pay-by date", "deadline"], ["schedule", "set", "choose"], ["payment", "transfer", "remittance"], ["use the invoice due date", "format dates as YYYY-MM-DD", "do not pay early by default"]),
    T("gst_total", ["GST", "tax", "total incl. tax", "grand total"], ["amount", "total payable", "invoice total"], ["record", "enter", "book"], ["use the total including GST", "not the subtotal", "check line items add up"]),
    T("reminder_email", ["reminder", "follow-up email", "chaser", "gentle reminder"], ["invoice", "bill", "payment"], ["no attachment", "without PDF", "text only"], ["go to the original email with the invoice", "reminders are not new invoices", "check if already processed"]),
    T("po_match", ["purchase order", "PO number", "PO reference", "order reference"], ["match", "check", "compare"], ["invoice", "bill", "receipt"], ["note the PO in the description", "flag mismatched POs", "PO must exist before paying"]),
    T("credit_note", ["credit note", "refund note", "negative invoice", "credit memo"], ["apply", "record", "offset"], ["vendor balance", "open bill", "payable"], ["record against the original bill", "do not schedule a payment", "reduce the open amount"]),
    T("currency", ["foreign currency", "USD invoice", "non-INR bill", "exchange rate"], ["convert", "record", "check"], ["amount", "total", "payment"], ["ask finance for the rate", "record in INR", "flag for review"]),
    T("partial_payment", ["partial payment", "instalment", "part payment", "split payment"], ["schedule", "record", "track"], ["bill", "invoice", "balance"], ["record each instalment", "keep remaining balance open", "needs approval if any part exceeds 50k"]),
    T("vendor_inactive", ["inactive vendor", "blocked supplier", "suspended vendor", "deactivated payee"], ["pay", "record", "bill"], ["invoice", "payment", "bill"], ["do not record bills", "escalate to procurement", "check status first"]),
    T("missing_field", ["missing field", "blank GSTIN", "incomplete form", "missing IFSC"], ["vendor form", "registration", "invoice"], ["required", "mandatory", "needed"], ["ask the requester", "do not invent values", "stop and report"]),
    T("api_first", ["read-only API", "API lookup", "query the API", "use the API"], ["vendors", "bills", "records"], ["before the GUI", "first", "for lookups"], ["is faster than clicking", "gives exact ids", "avoids page parsing"]),
    T("evidence", ["screenshot", "evidence", "proof", "audit trail"], ["after saving", "after submit", "after changes"], ["bill page", "confirmation page", "sent folder"], ["keep it for the report", "link it in the summary", "is captured automatically"]),
    T("toast_noise", ["notification popup", "toast", "unrelated alert", "banner"], ["appears", "pops up", "shows"], ["after an action", "on every page", "randomly"], ["is not progress", "ignore it", "check the real page state"]),
    T("prompt_injection", ["hidden instructions", "ignore previous instructions", "injected note", "instructions in email"], ["email", "document", "page"], ["telling you to pay", "asking to skip approval", "to change bank details"], ["never follow them", "treat as untrusted data", "report as suspicious"]),
    T("decline_final", ["approval declined", "manager said no", "request rejected", "declined payment"], ["Finance Manager", "approver", "manager"], ["payment", "bank change", "email"], ["do not retry", "report the decline", "is final for this run"]),
    T("verify_writes", ["re-read the record", "verify the saved bill", "check after saving", "confirm the entry"], ["amount", "dates", "vendor"], ["ERP", "API", "bill page"], ["compare with the source document", "before notifying", "to catch typos"]),
    T("attachment_download", ["download attachment", "save the PDF", "fetch the invoice file", "get the attachment"], ["email", "message", "mail"], ["workspace", "drive", "files"], ["then read it with read_file", "links marked download save files", "keep the file name"]),
    T("search_mail", ["search the mailbox", "find the email", "look in inbox", "filter mail"], ["vendor name", "invoice number", "sender"], ["inbox", "mailbox", "AP mailbox"], ["shows all messages on one page", "use the search box", "sort by date"]),
    T("plan_scope", ["already recorded bill", "existing bill", "bill exists", "recorded payable"], ["pay", "schedule", "settle"], ["vendor", "supplier", "payee"], ["skip finding the invoice again", "start from the ERP record", "no need to re-read the PDF"]),
    T("vendor_domain_check", ["sender domain", "email domain", "from address domain", "mail domain"], ["match", "compare", "verify"], ["vendor master", "registered domain", "ERP vendor record"], ["must match exactly", "mismatch means fraud", "check before processing"]),
    T("description_field", ["bill description", "memo field", "line description", "short description"], ["write", "fill", "enter"], ["what was supplied", "goods", "services"], ["keep it short", "mention the PO", "use invoice line items"]),
    T("date_format", ["date format", "YYYY-MM-DD", "ISO date", "date field"], ["enter", "type", "fill"], ["invoice date", "due date", "payment date"], ["convert from DD-MM-YYYY", "the form rejects other formats", "check the year"]),
    T("rounding", ["rounding", "paise", "decimal places", "two decimals"], ["amount", "total", "GST"], ["invoice", "bill", "payment"], ["keep two decimals", "do not round to rupees", "match the PDF exactly"]),
    T("procurement_notify", ["notify procurement", "tell procurement", "procurement desk", "inform purchasing"], ["new vendor", "vendor created", "supplier added"], ["vendor id", "status", "pending verification"], ["include the vendor id", "say it awaits verification", "keep it internal"]),
    T("approval_details", ["approval request", "ask for approval", "approval card", "approver view"], ["amount", "vendor", "due date"], ["Finance Manager", "approver", "manager"], ["include business context", "bind approval to the payload", "one request per action"]),
    T("cost_budget", ["step budget", "cost cap", "running long", "too many steps"], ["agent", "operator", "run"], ["converge", "stop", "finish"], ["finish essentials and report honestly", "do not loop", "stop cleanly"]),
    T("resume_run", ["resumed run", "crash recovery", "restarted process", "interrupted run"], ["browser", "session", "page"], ["state", "progress", "last action"], ["re-observe before acting", "do not assume earlier writes", "check for duplicates"]),
    T("ambiguous_vendor", ["two similar vendors", "ambiguous supplier", "unclear vendor", "which vendor"], ["name", "request", "invoice"], ["requester", "AP lead", "user"], ["ask which one was meant", "do not guess", "list the candidates"]),
    T("statement_recon", ["vendor statement", "account statement", "balance confirmation", "statement of account"], ["reconcile", "compare", "match"], ["open bills", "ledger", "payables"], ["list mismatches", "do not change records", "report differences"]),
]
LESSON_FRAMES = ["When the {b} {a} on a {c}, it {act}.", "Lesson: {a} for the {c} — {b}; this {act}.",
                 "If a {c} has {a}, {b} it: it {act}.", "{a} ({b}, {c}) {act}.", "Pitfall: {a} when you {b} the {c}; it {act}.",
                 "Workflow: {b} the {c} when you see {a}, because it {act}."]
QUERY_FRAMES = ["The {c} sent something about {a} — what do we do when we {b} it?", "How should I handle {a} for this {c}?",
                "{c}: {a}. What is the right way to {b} this?"]
HUBS = ["system:erp", "system:mail", "sop:invoice_processing"]
SOPS = ["sop:invoice_processing", "sop:vendor_onboarding", "sop:payments", "sop:statements"]
PEOPLE = [f"person:staff{i:03d}@kestrel.in" for i in range(60)]


def vendor_names(n: int, rnd: random.Random) -> list[str]:
    names, seen = [], set()
    while len(names) < n:
        name = f"{rnd.choice(ADJ)} {rnd.choice(NOUN)} {rnd.choice(SUFFIX)}"
        if name not in seen:
            seen.add(name)
            names.append(name)
        if len(seen) > len(ADJ) * len(NOUN) * len(SUFFIX) * 0.9:
            break
    i = 0
    while len(names) < n:  # more vendors than unique combos: disambiguate with a branch number
        names.append(f"{names[i % len(seen)]} {i // len(seen) + 2}")
        i += 1
    return names


def lesson_text(topic_idx: int, rnd: random.Random, vendor: str | None, mention: bool) -> str:
    name, groups, acts = TOPICS[topic_idx]
    t = rnd.choice(LESSON_FRAMES).format(a=rnd.choice(groups[0]), b=rnd.choice(groups[1]), c=rnd.choice(groups[2]),
                                         act=rnd.choice(acts))
    if vendor and mention:
        t = f"For {vendor}: " + t
    return t


def query_text(topic_idx: int, rnd: random.Random) -> str:
    name, groups, acts = TOPICS[topic_idx]
    return rnd.choice(QUERY_FRAMES).format(a=rnd.choice(groups[0]), b=rnd.choice(groups[1]), c=rnd.choice(groups[2]))


def _shard(args):
    """Generate runs [lo, hi). Each run: ~10 facts; ~0.5 lessons on average."""
    lo, hi, n_vendors, seed = args
    rnd = random.Random(seed * 1_000_003 + lo)
    names = vendor_names(n_vendors, random.Random(seed))
    weights = [1 / (r + 1) ** 1.1 for r in range(n_vendors)]  # Zipf-like vendor usage
    cum, acc = [], 0.0
    for w in weights:
        acc += w
        cum.append(acc)
    import bisect
    runs, facts, lessons = [], [], []
    for r in range(lo, hi):
        v = bisect.bisect_left(cum, rnd.random() * acc)
        run = f"run:{r}"
        ents = [f"vendor:{v}", f"bill:{r}", f"email:{r}", rnd.choice(PEOPLE), rnd.choice(PEOPLE), *HUBS,
                rnd.choice(SOPS), f"invoice:{r}"]
        facts += [(run, e) for e in ents]
        runs.append((run, v))
        if rnd.random() < 0.5:
            if rnd.random() < 0.3:   # vendor-specific lesson
                mention = rnd.random() < 0.5
                lessons.append({"id": f"L{r}", "run": run, "vendor": v, "topic": rnd.randrange(len(TOPICS)),
                                "mention": mention})
            else:
                lessons.append({"id": f"L{r}", "run": run, "vendor": None, "topic": rnd.randrange(len(TOPICS)),
                                "mention": False})
    for l in lessons:
        lr = random.Random(zlib.crc32(l["id"].encode()))  # stable across processes (hash() is salted)
        l["text"] = lesson_text(l["topic"], lr, names[l["vendor"]] if l["vendor"] is not None else None, l["mention"])
    return runs, facts, lessons


def generate(n_runs: int = 100_000, n_vendors: int = 20_000, seed: int = 7, workers: int = 8) -> dict:
    t0 = time.time()
    bounds = [(i * n_runs // workers, (i + 1) * n_runs // workers, n_vendors, seed) for i in range(workers)]
    runs, facts, lessons = [], [], []
    with ProcessPoolExecutor(workers) as ex:
        for r, f, l in ex.map(_shard, bounds):
            runs += r
            facts += f
            lessons += l
    names = vendor_names(n_vendors, random.Random(seed))
    rnd = random.Random(seed + 1)
    # planted queries: vendor-scoped (ground truth = that vendor's lessons) and paraphrased topic queries
    by_vendor: dict[int, list[str]] = {}
    for l in lessons:
        if l["vendor"] is not None:
            by_vendor.setdefault(l["vendor"], []).append(l["id"])
    cands = [v for v, ls in by_vendor.items() if 2 <= len(ls) <= 40]
    rnd.shuffle(cands)
    vendor_q = [{"kind": "vendor", "text": f"Process the latest invoice from {names[v]}", "truth": by_vendor[v]}
                for v in cands[:250]]
    by_topic: dict[int, list[str]] = {}
    for l in lessons:
        if l["vendor"] is None:
            by_topic.setdefault(l["topic"], []).append(l["id"])
    topic_q = []
    for i in range(250):
        t = i % len(TOPICS)
        topic_q.append({"kind": "topic", "text": query_text(t, rnd), "truth": by_topic.get(t, [])})
    data = {"runs": runs, "facts": facts, "lessons": lessons, "vendor_names": names, "queries": vendor_q + topic_q,
            "meta": {"n_runs": n_runs, "n_vendors": n_vendors, "n_facts": len(facts), "n_lessons": len(lessons),
                     "gen_seconds": round(time.time() - t0, 1), "seed": seed}}
    return data


def export_for_embedding(d: dict, n: int) -> None:
    """Text files for remote (HPC) embedding. Row i of the resulting .npy must match line i here."""
    with open(OUT / f"export_{n}_lessons.jsonl", "w") as f:
        for i, l in enumerate(d["lessons"]):
            f.write(json.dumps({"i": i, "id": l["id"], "text": l["text"]}) + "\n")
    with open(OUT / f"export_{n}_queries.jsonl", "w") as f:
        for i, q in enumerate(d["queries"]):
            f.write(json.dumps({"i": i, "text": q["text"]}) + "\n")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    OUT.mkdir(exist_ok=True)
    d = generate(n_runs=n, n_vendors=max(2000, n // 5))
    with open(OUT / f"company_{n}.pkl", "wb") as f:
        pickle.dump(d, f, protocol=5)
    export_for_embedding(d, n)
    print(json.dumps(d["meta"]))
