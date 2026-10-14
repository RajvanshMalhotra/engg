# Submission form — drafted answers

Form: CentrAlign AI Engineer Hiring Form. Personal fields (1–11) are yours to fill. Links (12–14) once published.
Every claim below is backed by code or a results file in this repo — keep it that way if you edit.

---

**6. Role:** Founding Engineer

**12. GitHub repository link:** https://github.com/RajvanshMalhotra/engg

**13. Live demo link:** _(optional — leave blank, or a recorded console walkthrough if hosted)_

**14. Demo video link:** _[after recording]_

---

### 15. In 3–5 sentences, what did you build?

I built an autonomous operator that turns a one-line company request ("Process the latest invoice from Acme") into
completed, independently verified work inside a company's own systems — a mail inbox, a finance ERP and documents —
using a real browser, read-only APIs, files and vision. Before acting, it reads the company's SOPs and writes a task
contract (outcome, constraints, success criteria); a deterministic policy engine, not the model, decides what needs
human approval; and the run cannot finish until a separate read-only verifier confirms the outcome against real system
records. It ships with a sandbox company full of traps (phishing with prompt injection, scanned invoices, duplicates,
injectable UI faults), an operator console with live approvals and a run inspector, and an eval suite scored on
ground-truth database state, three runs per task: 36/36 on the development tasks (clean and with all faults on) and
21/21 on a held-out set of tasks never used for tuning (6/7 on the first pass — the miss was a harness bug, since
fixed), at about $0.08–0.10 (off-peak pricing) and 3–4 minutes per run.

### 16. Briefly explain your architecture

Request → **Contract**: the model picks relevant SOPs, reads them and writes outcome, constraints, success criteria and
subgoals before acting. → **Agent loop** (hand-written, no framework): each turn sees a stable cached prompt plus a pinned
working state (plan, notes, warnings); tools are a browser that renders pages as text with element ids and reports the
state change after every action, a read-only ERP API, files (PDF text, vision for scans), and company documents.
→ **Policy engine**: YAML rules (allow / require approval / deny) enforced in code before every form submit and again at
the network layer, with approvals bound to the exact payload and declines remembered; secrets are typed by tools and
never enter the model's context. → **Verifier**: a separate, read-only, clean-context agent that checks the contract's
criteria against real records, an objective before/after diff of system state and the audit log of approvals; failing
sends feedback back to the loop. → **Evidence**: append-only event log, screenshots and a report per run; checkpoints
make runs resumable. → **Memory**: lessons distilled after verified runs, grounded in the SOPs, by a background worker
pool. Models are routed per role (worker, verifier, planning, vision) in `models.yaml` through a provider-adapter layer;
DeepSeek is the only provider run end to end so far.

### 17. What parts of your system are genuinely autonomous?

Everything between the one-line request and the verified result: deciding which company procedures apply and what
"done" means; finding the right email or record (e.g. recognising the newest Acme email is a reminder and going to the
one with the invoice); choosing between API, browser and vision; reading PDFs and scanned images; logging in; filling and
submitting forms; detecting failures (server errors, renamed UI, expired sessions, decoy notifications) and recovering;
noticing when work is already done (duplicate invoices) or when a request is fraudulent and stopping to report it;
deciding when a human is genuinely needed; writing the report; and learning lessons afterwards. Humans only approve what
policy requires (₹50k+ payments, bank changes, external email) or answer genuinely ambiguous questions. With all four
faults injected at once it completed 6/6 development tasks and the verifier agreed with ground truth every time.

### 18. What is currently hard coded or manually configured?

- The **company environment** is a sandbox I built (mail + ERP in FastAPI/SQLite) with seeded data and traps.
- **Company knowledge** is hand-written data: 4 markdown docs (two SOPs, a finance policy, a directory),
  `company/systems.yaml` (system URLs, the ERP API's endpoints, which secrets to use for login) and
  `company/policy.yaml` (5 rules; conditions use two built-in checks: payment amount threshold, external recipients).
- **Model routing** per role in `models.yaml`; the system prompt's operating principles; per-run step and cost caps.
- **Evals**: task lists, ground-truth checks and reference step counts are hand-written; the eval's human approvals and
  answers are scripted.
- **UI heuristics**: the page-to-text converter's rules and the console's screenshot labels.
Nothing task-specific is in the runtime: there is no invoice or vendor code path — new workflows are added as SOPs and
registry entries.

### 19. What models, frameworks, APIs, libraries, AI coding tools, or existing projects did you use?

Models: DeepSeek V4 Pro (worker, verifier, planning) and DeepSeek Flash (vision) via the OpenAI-compatible API — all
results and the demo use DeepSeek only. The repo also contains an untested scaffold for a Claude adapter (Anthropic SDK,
checked only against mocked responses, never run on the live API); making the harness truly model-agnostic is future
work (see 21).
Libraries: Playwright (browser automation), FastAPI + uvicorn + Jinja2 + SQLite (sandbox apps and console), pypdf
(PDF text), reportlab and Pillow (generating the seeded invoices and the scanned image), httpx, PyYAML, python-dotenv,
rich, pytest, uv. Experiments only: Qdrant 1.19 (vector DB) and fastembed with BAAI bge-small embeddings. No agent
framework (LangChain/LangGraph/CrewAI etc.), no templates or pre-built agent projects. AI coding tool: built with Claude
Code; I can explain, debug and modify every module.

### 20. What is the biggest technical limitation of your current solution?

It is proven only in a company I built: two systems whose UIs, quirks and data I control. Real enterprise software adds
single sign-on and MFA, complex single-page apps with iframes and shadow DOM, rate limits and far messier documents, and
the page-to-text observation and policy rules would need hardening against those. Two smaller ones: the verifier is
itself an LLM (mitigated by an objective record diff and programmatic eval checks, but a different model family should
audit), and resume after a crash is not exactly-once — a write between the last checkpoint and the crash could repeat
(the verifier would flag a duplicate; the real fix is idempotency keys).

### 21. If you had another 2 weeks, what would you build/change next?

1) Make it genuinely model-agnostic: finish and live-test the Claude adapter (and one more provider), then run the
same eval across providers with a verifier from a different model family than the worker; 2) connect to real systems instead of only the sandbox — Gmail/Outlook APIs and an open-source ERP (ERPNext or Odoo) —
and harden observation for real SaaS UIs; 3) desktop apps through OS accessibility APIs, with pixel-level computer use as
the last resort; 4) idempotency keys on every write for exactly-once resume, plus a queue of stateless workers with a
browser pool (runs are already event-sourced); 5) approvals in Slack/Teams with signed, payload-bound approval tokens and policy in OPA/Cedar per tenant; 6) a larger
eval in CI with a red-team set of injection pages and emails, and promotion of repeatedly verified workflows into
reusable skills.

---

### 22. Final declaration — all five are true for this submission
- Built and understand it: yes (see `docs/INTERVIEW_PREP.md`).
- Pre-built components/templates disclosed: none used beyond the libraries listed in 19.
- AI coding tools disclosed: Claude Code.
- No confidential data or unauthorized third-party access: all data is synthetic; the sandbox is local.
- Comfortable debugging/modifying live: the live-debug cheat sheet is in `docs/INTERVIEW_PREP.md` §13.
