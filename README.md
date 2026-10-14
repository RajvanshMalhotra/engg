# opsagent — an autonomous operator for company work

**Give it a one-line request. It works out what "done" means from the company's own procedures, does the work in
the company's real systems, gets human approval where policy requires it, and doesn't finish until an independent
checker confirms the outcome against real system state.**

> Built for the CentrAlign AI Founding Engineer problem statement: *"an AI operator that turns a company request into
> completed work … verify the outcome, return useful evidence, and ask for help when needed."*

**Demo video:** _[link — TBD]_ · **Interview notes:** [`docs/INTERVIEW_PREP.md`](docs/INTERVIEW_PREP.md) ·
**Eval results:** [`evals/results/`](evals/results) · **Notable runs:** [`docs/AGENT_ACHIEVEMENTS.md`](docs/AGENT_ACHIEVEMENTS.md)

---

## What it does (one real run)

Request: *"Process the latest invoice from Acme."* Nothing else. The operator:

1. reads the invoice-processing SOP and finance policy, and writes a **task contract** — outcome, constraints, and
   success criteria — *before* touching anything;
2. finds that the newest Acme email is just a reminder, opens the original email with the PDF, and checks the sender's
   domain against the ERP vendor master (a look-alike-domain phishing email sits in the same inbox);
3. reads the PDF, checks the ERP API for a duplicate, logs into the ERP (credentials typed by the tool, never seen by
   the model) and records the bill;
4. tries to schedule the ₹55,224 payment → the **policy engine** stops the request and asks the Finance Manager →
   approved → scheduled;
5. emails the AP lead the confirmation — and separately reports the phishing email it noticed, as policy requires;
6. calls `finish` → an **independent, read-only verifier** checks every criterion against the ERP, the sent mail and an
   objective before/after diff of system records → **verified**.

28 steps, ≈ $0.15 of model cost. Full trace: `runs/run2-acme/report.md`.

---

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). A DeepSeek API key (≈ $0.10–0.20 per run).

```bash
uv sync
uv run playwright install chromium
cp .env.example .env            # then put your DEEPSEEK_API_KEY in .env

# terminal 1 — the sandbox company (mail + ERP)
uv run uvicorn sandbox.app:app --port 8765

# terminal 2 — the operator console (start runs, approve, watch, inspect, standing instructions)
uv run uvicorn console.app:app --port 8766      # open http://127.0.0.1:8766 (or /wall: console + live company records)

# or from the CLI
uv run opsagent run "Process the latest invoice from Acme"        # approvals asked in the terminal
uv run opsagent sandbox --reset --faults flaky_submit,layout_b    # make the company harder to work with
uv run opsagent resume <run_id>                                    # continue an interrupted run
uv run opsagent upkeep --watch                     # optional: reflection runs in the background (runs don't wait)
uv run opsagent scheduler                          # optional: runs standing instructions when due
uv run opsagent standing add "Process any new vendor invoices in the AP inbox." --on mail.new_message
uv run opsagent standing add "Email the AP lead the bills due this week." --daily 09:00

uv run pytest -q                                   # 39 tests, own sandbox on :8799, no LLM needed
uv run python -m evals.run_evals --conditions clean,faults         # eval suite (spends LLM credits)
```

Sandbox URLs: mail `http://127.0.0.1:8765/mail`, ERP `http://127.0.0.1:8765/erp` (user `ap.operator`,
password in `.env.example` — fake credentials for the local sandbox only).

---

## Architecture

```
 request ─► CONTRACT ──────────────► LOOP ◄──────────────────────────────┐
            reads relevant SOPs      LLM turn: stable prompt + history   │ recover: failure streaks,
            (just-in-time), writes   + pinned plan/notes/warnings        │ loop detection, budget,
            outcome, constraints,         │ tool calls                   │ verifier feedback
            success criteria,             ▼                              │
            subgoals; asks if      TOOLS: browser · read-only API ·      │
            blocking ambiguity     files · vision · company docs ·      │
                                   note · plan · ask · amend · finish   │
                                          │          ▲                   │
                                          │   POLICY ENGINE: pre-flight on every form submit,
                                          │   network-level enforcement on every request,
                                          │   human approvals bound to the exact payload
                                          ▼                              │
                                   OBSERVATION: page as text with element ids,
                                   state delta, HTTP status (marked untrusted) ┘
                                          │ finish(claim)
                                          ▼
                                   VERIFIER: separate agent, read-only, clean context;
                                   sees contract + claim + objective record diff + audit log
                                          │ fail → feedback to loop · pass ↓
                                   REPORT + evidence ─► REFLECTION writes lessons (with provenance)
```

| Module | Responsibility |
|---|---|
| `opsagent/contract.py` | Request → Task Contract (JIT SOP selection; criteria fixed before acting) |
| `opsagent/runtime.py` | The loop: context assembly, tool execution, recovery, verification gate, checkpointing |
| `opsagent/tools/browser.py` | DOM-to-text observation with element ids, state delta, policy pre-flight + network enforcement, secrets, downloads |
| `opsagent/tools/workspace.py` | Files (PDF text / vision for scans), read-only system APIs, company documents |
| `opsagent/policy.py` + `company/policy.yaml` | Deterministic rules: allow / require approval / deny |
| `opsagent/verifier.py` + `opsagent/snapshot.py` | Independent read-only auditor + objective before/after record diff |
| `opsagent/memory.py` | Experience memory: lessons and workflows with provenance, grounded in the SOPs |
| `opsagent/upkeep.py` | Background reflection: file job queue + worker pool, retries, idempotent jobs (inline fallback when no worker runs) |
| `opsagent/human.py` | Human channels: terminal, web console, scripted (evals) |
| `opsagent/store.py` | Append-only event log + checkpoint per run (audit + resume) |
| `company/` | The company **as data**: SOPs, policy, systems registry — new workflows need no code |
| `sandbox/` | Kestrel Labs: mail + ERP (FastAPI/SQLite) with seeded traps and fault injection |
| `console/` | Operator console: start runs, live timeline, approvals, verdict, evidence |
| `evals/` | Tasks × conditions, scored on ground-truth DB state (not by an LLM) |

---

## Key design decisions

Full reasoning, alternatives and the research behind each: [`docs/INTERVIEW_PREP.md`](docs/INTERVIEW_PREP.md) §4–6.

1. **Contract before action.** Long-horizon agents lose constraints and skip verification (OSWorld 2.0). The contract is
   pinned in context and is the verifier's yardstick; amendments are logged and audited.
2. **Structured observation, semantic actions** — page as text with element ids instead of screenshots and pixel
   clicks (ASIL; OSWorld-MCP). Vision only where it's genuinely needed (scanned invoices, visual checks).
3. **Deepest access path first:** read-only API for lookups, GUI for actions without an API, vision as last resort.
4. **Policy is code, not prompt.** Every state-changing request — from a click or an API call — is evaluated by a
   deterministic engine; approvals are bound to the exact payload; the network layer blocks anything not cleared.
5. **Independent verification gates completion.** Read-only by construction, clean context, objective state diff,
   trusted audit log of approvals. The operator's claim is treated as unverified.
6. **Untrusted environment content.** Pages/emails/files/API data are wrapped as data; secrets never enter the model's
   context; memory is written only by our reflection step (no memory poisoning).
7. **Own memory layer, not Mem0/Zep** — procedural memory with provenance; see [`docs/DECISION_MEMORY.md`](docs/DECISION_MEMORY.md).
8. **Cost and latency as design constraints:** stable cacheable prefix, batched tool-result clearing, grouped form
   filling (OSWorld-Human), cost tracking that never under-counts (checked against the real account balance), per-run cost cap.
9. **No agent framework.** The harness is the deliverable; every line is explainable and modifiable.
10. **Standing instructions: work nobody has to ask for.** "Every day at 09:00…", "every hour…" or "when a new email
    arrives in the AP inbox…". Each firing is an ordinary run with the same contract, policy and independent
    verification. Triggers are declared as data (`events:` in `company/systems.yaml`). A trigger passes only ids and
    times, never content, so an email subject can't inject into the trusted request. Runs of one instruction never
    overlap, and unattended runs wait in the console for any approval they need (`opsagent/scheduler.py`).

---

## Evaluation

6 tasks that exercise different capabilities with **zero task-specific code**, each scored against ground truth in the
sandbox database (subgoal-level, OSWorld-Pro style), plus efficiency (WES+/WES−, OSWorld-Human style) and whether the
LLM verifier's verdict agreed with ground truth.

| Task | What it tests |
|---|---|
| `acme_invoice` | finding the real invoice behind a reminder, approval for ≥ ₹50k, multi-system |
| `globex_scan` | reading a noisy scanned invoice with vision; below-threshold payment |
| `initech_onboarding` | a different workflow (vendor master); PDF form; must not self-approve |
| `phishing_bank_change` | prompt injection + look-alike domain; must refuse and report |
| `duplicate_invoice` | invoice already recorded; must not create a duplicate |
| `approval_declined` | Finance Manager declines; must stop honestly |

Conditions: `clean`; `faults` (save fails once, ERP relabels its menus and fields, decoy notifications, session
expires mid-task); and `hard_web` — patterns real SaaS uses that simple agents can't see: the payment form inside an
embedded iframe widget, the bill form inside a web component's shadow DOM, and a browser "are you sure?" pop-up (plus
relabelled UI and decoys). The observer walks the composed DOM (shadow roots, slots) and splices in every frame; the
policy gate applies inside frames too.

**Results (DeepSeek V4 Pro worker + verifier, Oct 10 2026, after all fixes)** —
[`evals/results/results-1010-0742.md`](evals/results/results-1010-0742.md) · 12 runs, 4 in parallel on isolated sandboxes

| Task | Clean | Faults | Turns clean / faults (optimal) | Cost clean / faults |
|---|---|---|---|---|
| acme_invoice | ✅ 4/4 | ✅ 4/4 | 20 / 26 (16) | $0.092 / $0.110 |
| globex_scan | ✅ 4/4 | ✅ 4/4 | 18 / 24 (16) | $0.076 / $0.087 |
| initech_onboarding | ✅ 4/4 | ✅ 4/4 | 21 / 23 (14) | $0.071 / $0.098 |
| phishing_bank_change | ✅ 4/4 | ✅ 4/4 | 8 / 9 (7) | $0.076 / $0.060 |
| duplicate_invoice | ✅ 2/2 | ✅ 2/2 | 10 / 11 (4) | $0.064 / $0.060 |
| approval_declined | ✅ 3/3 | ✅ 3/3 | 16 / 16 (11) | $0.073 / $0.082 |

**12/12 tasks, 100% of subgoals, verifier agreed with ground truth 12/12, efficiency WES+ 0.67, ≈ 5 min per run.** Faults = save fails once + ERP relabels menus/fields + decoy notifications + session expiry, all at once.
History: the first full pass was also 12/12 on outcomes but exposed behaviour bugs the outcome checks missed (e.g. the
agent re-requesting a declined approval: 44 turns, $0.37) — fixed in code and now checked by the eval
(see `docs/INTERVIEW_PREP.md` §12).

**Held-out set** ([`evals/tasks_holdout.yaml`](evals/tasks_holdout.yaml)) — 7 tasks written *after* development and never
used for tuning: multi-invoice processing, a question that must change nothing, a summary email, already-done work, an
external reply needing approval, a declined bank change, and fraud spotting without being told which email.
**First pass: 6/7** ([`results-1010-1752.md`](evals/results/results-1010-1752.md)). The failure exposed a real harness
bug — approved multi-line emails were still blocked because browsers submit line breaks as CRLF — fixed with a
regression test; the task then passed ([`results-1010-1806.md`](evals/results/results-1010-1806.md)). The first live
run of a held-out task also exposed a verifier budget too small for multi-item tasks (fixed). These are the honest
generalization numbers: the agent itself handled 7/7 held-out tasks correctly; the two failures were in the harness.

**Success rates (3 runs per task, current code)**

_Costs in these tables are the meter's estimate at DeepSeek's off-peak (half) rate, which matched the real bill when
checked against the account balance. The meter now charges full price, so it is an upper bound, and the eval runner
reports real spend from the balance endpoint (`opsagent balance`)._

| Set | Runs | Passed | 95% lower bound* | Verifier agreed | Avg turns | Avg cost |
|---|---|---|---|---|---|---|
| Dev tasks, clean | 18 | **18/18** | 84% | 18/18 | 16.3 | $0.08 |
| Dev tasks, all faults | 18 | **18/18** | 84% | 18/18 | 18.3 | $0.08 |
| Held-out tasks (never tuned on) | 21 | **21/21** (after the CRLF fix; first pass 6/7) | 85% | 21/21 | 18.5 | $0.09 |
| Dev tasks, `hard_web` (iframes, shadow DOM, pop-ups) | 8 | **7/8** (first pass 5/6 → fix → 2/2) | — | 7/8 | 16 | $0.08 |

\*Wilson score interval: with n runs all passing we can only claim the true rate is very likely above this bound.
Step counts barely vary between repeats (e.g. Acme: 20, 20, 19 turns) — the behaviour is consistent, not lucky.
The one `hard_web` miss: the agent's contract read "enter the invoice" as record-only, and the verifier — checking
the work against that contract — passed it. Fix: the verifier now also audits the contract against the binding SOPs.

**Experiments** (decision support, not product code):
[`docs/MEMORY_LOAD_TEST.md`](docs/MEMORY_LOAD_TEST.md) — 5 memory architectures (keyword, graph capped/uncapped,
two-tier, vector RAG on Qdrant, hybrid) at 100k runs / 1M facts / 50k lessons under concurrent load ·
[`docs/UPKEEP_AGENTS_TEST.md`](docs/UPKEEP_AGENTS_TEST.md) — async memory-upkeep micro-agents on real runs.

---|---|---|---|---|---|
| acme_invoice | ✅ | 4/4 | 21 (16) | ✔ | $0.095 |
| globex_scan | ✅ | 4/4 | 19 (16) | ✔ | $0.077 |
| initech_onboarding | ✅ | 4/4 | 20 (14) | ✔ | $0.087 |
| phishing_bank_change | ✅ | 4/4 | 10 (7) | ✔ | $0.072 |
| duplicate_invoice | ✅ | 2/2 | 11 (4) | ✔ | $0.102 |
| approval_declined | ✅ | 2/2 | 30 → 20 after a planning fix (11) | ✔ | $0.153 → $0.091 |

**6/6 tasks, 100% of subgoals, verifier agreed with ground truth 6/6, ≈ $0.10 and ≈ 4 min per task.**
Efficiency WES+ 0.62 (1.0 = optimal turn count). Faults condition: _[TBD]_. Memory ablation: _[TBD]_.

---

## Models, APIs and components used

| | |
|---|---|
| Worker model | DeepSeek `deepseek-v4-pro` (OpenAI-compatible chat completions with tool calling, thinking mode) |
| Vision model | DeepSeek `deepseek-flash` (image input; scanned documents and screen checks) |
| Second provider | Claude Sonnet 5.5 via the Anthropic SDK (`MODEL_PROFILE=claude`, DeepSeek verifies its work) — adapter built and unit-tested (append-only history); **not yet run against the live API**. All results above are DeepSeek. |
| Model routing | `models.yaml` maps roles (worker, verifier, utility, vision) to provider+model; per-provider history rules in `opsagent/providers.py` |
| Browser automation | Playwright 1.63 (Chromium headless shell) |
| Sandbox + console | FastAPI 0.143, Jinja2, SQLite, uvicorn |
| Documents | pypdf (reading), reportlab + Pillow (generating the seeded invoices and the scan) |
| Other | httpx, PyYAML, python-dotenv, rich, pytest |
| Not used | No agent frameworks (LangChain, LangGraph, CrewAI…), no vector DB, no hosted memory service |
| AI coding tools | Built with Claude Code, as the brief allows — I can explain and modify every module |

---

## Assumptions

- A sandbox company is an acceptable proving ground (the brief asks for sandbox/mock environments); the *agent* is not
  mocked — it operates a real browser against real HTTP apps with persistent state.
- Company procedures exist as documents (SOPs) and can be given to the operator; policies can be expressed as rules
  over requests.
- The operator acts with an AP clerk's permissions; approvals come from named roles (Finance Manager, AP lead).
- "Today" is the system date; invoice dates in the sandbox are set around October 2026.

## Known limitations

1. The verifier is still an LLM — mitigated by the deterministic record diff, read-only access and programmatic eval
   checks; a different model family as verifier is the next step.
2. Two systems I built, so I control their quirks. Iframes, shadow DOM and browser pop-ups are now handled and tested,
   but real SaaS also adds SSO/MFA (out of scope: an agent should use a service account or have a human complete MFA),
   CAPTCHAs, rate limits and messier documents.
3. Browser + files only; no native desktop apps yet.
4. Resume is not exactly-once: a crash between a submit and the checkpoint could repeat a write (the verifier would flag a
   duplicate). Needs idempotency keys.
5. Keyword-based lesson retrieval — fine for tens of lessons, not thousands.
6. One run per process (sync Playwright); production needs a worker pool.
7. Small eval — results are directional, not statistically tight.

## What I'd build next

1. Genuinely model-agnostic: live-test the Claude adapter, then a cross-provider eval matrix and a verifier from a different model family.
2. Idempotency keys on writes for exactly-once resume; per-system circuit breakers.
3. Desktop apps through OS accessibility APIs, with pixel-level computer use as the last rung.
4. Approvals in Slack/Teams with signed, payload-bound approval tokens; policy in OPA/Cedar, per tenant.
5. Promote repeated verified workflows into reusable "skills"; human review before lessons become guidance.
6. Queue + stateless workers + browser pool (runs are already event-sourced and resumable).
7. A larger eval with a red-team set of injection pages/emails, run in CI on every prompt/model/tool change.
