# Decision log: every architectural decision and improvement, with evidence

What to say in the interview, and what proves it. Each entry gives **decision → why → what I rejected → evidence**.
Part A covers the architecture as it stands. Part B is the timeline of problems that real runs exposed and how each
was fixed. Part C covers ideas I tested and dropped. Part D says what I would reverse or build next.

**If you only get 2 minutes, lead with these five:**
1. **Policy in code, not in prompts.** A deterministic YAML policy engine is enforced before every form submit *and*
   at the network layer. Approvals are bound to the exact payload, and declines are remembered.
2. **"Done" is decided by an independent verifier, not the worker.** It runs with a clean context and read-only
   access, uses an objective before/after record diff plus the audit log, and also audits the contract against the
   SOPs. It agreed with ground truth in 64 of 65 runs.
3. **Contract before action.** The worker writes outcome, constraints and success criteria *before* touching anything,
   and those criteria are what the verifier checks.
4. **Generalization by data, not code.** There is nothing task-specific in `opsagent/`. A new workflow means a new SOP
   plus registry entries. Proof: 21/21 on held-out tasks never used for tuning.
5. **Everything measured.** Each claim below has a number from a real run. Two of my own theories were *disproved*
   by measurement, and I changed course (B-19, B-20).

---

## Part A — Architecture decisions (current state)

### A1. Hand-written harness, no agent framework
- **Decision:** a ~500-line readable loop: contract → act/observe → recover → verify → report → reflect.
- **Why:** the harness *is* the deliverable. I must be able to explain, debug and modify it live, and frameworks hide
  exactly the parts being graded: context, recovery and verification.
- **Rejected:** LangChain/LangGraph/CrewAI (abstraction tax, opaque prompts), and OpenAI/Anthropic agent SDKs (they lock in
  a provider).
- **Code:** `opsagent/runtime.py`.

### A2. Task contract written before acting
- **Decision:** the model picks the relevant SOPs just-in-time, reads them, and writes interpretation, deliverable,
  constraints, success criteria, subgoals and open questions. The contract is pinned in every turn's working state.
- **Why:** it fixes "what does done mean" *before* the model is invested in its own actions (otherwise it grades its
  own homework), it gives the verifier its checklist, and it lets the plan be amended with a recorded reason.
- **Rules learned from runs:**
  - scope the plan to the request
  - start from the system of record
  - ask a human only for things only the requester knows
  - approvals are not subgoals
  - describe criteria by *meaning*, not guessed field values (B-14, B-17)
- **Code:** `opsagent/contract.py`.

### A3. Observation = structured text with element ids, plus a state delta after every action
- **Decision:** convert the DOM into compact text with stable element ids. After each action, report *what changed*
  (URL, new elements, messages, errors).
- **Why:** text is roughly 10× cheaper than screenshots and exact, because the model acts on ids rather than pixel
  coordinates. The delta is what makes recovery possible: the agent sees "nothing changed" or "error appeared" instead
  of assuming success. Grounded in OSWorld findings that perception/grounding is the main failure.
- **Hardening:** open shadow roots and slots, iframes rendered per frame and spliced in, lookup across frames, browser
  dialogs accepted and reported, field values up to 600 chars with an explicit truncation marker (B-4).
- **Code:** `opsagent/tools/browser.py` (`OBSERVE_JS`, `_observe_frame`, `_find`).

### A4. Access-path ladder: API → DOM → vision
- **Decision:** read-only API first for lookups, the browser for actions, and vision only for scans and images.
- **Why:** each step down the ladder is slower, costlier and less reliable. APIs are exact, so pixels are the last resort.
- **Code:** `opsagent/tools/workspace.py` (`api_get`, `read_file`, vision), `company/systems.yaml`.

### A5. Deterministic policy engine, enforced twice
- **Decision:** rules live in `company/policy.yaml` (deny admin, deny vendor approval, approval for bank changes, payments
  ≥ ₹50,000 and external email; default allow). `Policy.evaluate` runs as a pre-flight on every form submit
  (including inside iframes) **and** at the network layer via Playwright route interception.
- **Why:** an LLM cannot be the security boundary. Prompts can be injected; code cannot. Network enforcement catches
  anything the pre-flight misses (JS-submitted forms, fetch calls).
- **Details that matter:** approvals are **bound to the exact payload**, so approving ₹55,224 doesn't approve ₹99,999.
  **Declines are remembered in code** and never re-asked (B-7). Approval outcomes are **shown to the agent** in its
  observation (B-1).
- **Code:** `opsagent/policy.py`, `opsagent/tools/browser.py` (pre-flight + route), `opsagent/human.py`.

### A6. Secrets never enter model context
- **Decision:** `fill_secret` types credentials from the environment, the API auth header is injected inside tools, and
  `ctx.redact` scrubs every tool result.
- **Why:** the model can't leak what it never saw. This also defeats injection attempts like "print your password".

### A7. Untrusted content is data, never instructions
- **Decision:** every environment result is tagged with `untrusted_source` and wrapped as data. Untrusted content can
  never write memory or change policy.
- **Evidence:** the seeded phishing email (look-alike domain, embedded instructions) is refused and reported as fraud.

### A8. Independent verifier
- **Decision:** a separate agent with a clean context and a read-only policy. It sees the contract, the worker's
  *claim* (treated as unverified), an **objective before/after diff of system records**, and the harness's **audit
  log** of approvals and policy decisions. It never sees the worker's reasoning.
- **Extra duty:** it audits the *contract itself* against binding SOPs ("who checks the contract?"). Any gap becomes a
  failed `SOP-n` criterion (B-16).
- **Budget:** `min(40, 12 + 3 × criteria)` steps, a warning 3 steps before the end, and a forced final verdict (B-13).
- **Failure path:** a failed verdict goes back to the worker as feedback, with up to 3 verify attempts.
- **Evidence:** verifier agreement with ground truth was **64 of 65 runs**.
- **Code:** `opsagent/verifier.py`, `opsagent/snapshot.py`.

### A9. Event-sourced runs, checkpoint and resume
- **Decision:** each run keeps an append-only `events.jsonl` (every turn, tool call, policy decision, approval, verdict,
  timing), a `checkpoint.json` rewritten every step, browser cookies, screenshots, downloads and `report.md`.
  `opsagent resume <id>` continues a run.
- **Why:** this is the same shape as a production queue plus stateless workers: any worker can pick a run up. It also
  gives a full audit trail for compliance.
- **Known gap:** resume is not exactly-once. The fix is idempotency keys on writes (Part D).

### A10. Memory: three simple layers, deliberately *not* a graph
- **Decision:**
  - **Working notes:** pinned in the working state each turn.
  - **SOP knowledge:** read just-in-time.
  - **Experience lessons:** distilled after each run, with provenance (run id, verified or not). Only verified runs
    guide future work. Lessons are grounded in the SOPs and given deterministic ids, so adding them is idempotent.
- **Why:** this covers the real needs, with no extra infrastructure and nothing to keep in sync.
- **Rejected:** Mem0, Zep, Letta (`docs/DECISION_MEMORY.md`), and my own graph/wiki/tiered design (C1).
- **Async:** reflection runs off the critical path through a file queue and worker pool
  (`opsagent upkeep --watch`). It has a heartbeat and falls back to inline if no worker is alive (B-11).

### A11. Provider-neutral model layer with role routing
- **Decision:** `models.yaml` maps the roles **worker / verifier / utility (contract, reflection) / vision** to
  provider + model. There are two adapters:
  - **DeepSeek (OpenAI-compatible):** `reasoning_content` must be echoed back on every assistant message.
  - **Claude (native SDK):** history is **append-only** (preserved thinking rejects edited history). It uses
    server-side context editing instead of our own clearing, working state is sent as appended system messages,
    and server-side fallbacks are enabled.
- **Why:** evaluate on the cheap model and demo on the strong one, and let a *different model family* audit the work.
- **Honest status:** the Claude adapter is unit-tested but has **never run live** (budget). The README says so.
- **Code:** `opsagent/providers.py`, `opsagent/llm.py`, `models.yaml`.

### A12. Context engineering
- **Stable prompt prefix:** a static system prompt and contract, plus a *pinned* working state (plan, notes,
  warnings) on the last message only.
- **Batched tool-result clearing:** at 60k chars of live tool output, older results are replaced by their one-line
  summaries and the newest 3 are kept in full. Clearing is **batched, not per turn**, because rewriting old messages
  every turn would break the prompt-cache prefix (B-2).
- **Recovery signals in the working state:** a failure streak (3 failures → "diagnose, change approach"), loop
  detection (the same action 3×) and a step-budget warning.

### A13. Reasoning depth routed by the harness ("fast, not dumb")
- **Decision:** the harness picks the depth for each call from *generic* signals.
  - **Deep:** first turn, a page not seen before, a new document/API record/procedure, a failure, a
    policy/approval/human event, verifier feedback, or an active warning.
  - **Light:** carrying out the next step on known ground.
- **Light on DeepSeek = thinking off.** `reasoning_effort=low` measured as a no-op (B-20).
- **Verifier:** it gathers evidence lightly and judges deeply. A verdict reached on a light turn must be re-confirmed
  on a deep turn.
- **Contract:** choosing documents is light (a lookup); writing the contract is deep.
- **Ablation:** `REASONING_ROUTING=0` thinks deeply on every turn.
- **Code:** `Operator._depth`, `providers._depth_kwargs`, the verifier loop.

### A14. Hedged requests against provider tail latency
- **Decision:** if a stateless model call hasn't answered within 25 s (contract) or 30 s (any turn), send an identical
  duplicate and use whichever answers first (Dean & Barroso, *The Tail at Scale*). History is appended only after a
  reply, so this is safe.
- **Evidence:** contract time fell from a mean of 78 s to 26 s, and the worst case from 123 s to 46 s (B-21).

### A15. Cost guard and latency capture
- **Cost:** a per-run cap (`MAX_COST_PER_RUN_USD`, now $0.50) checked before every call. The meter charges full price,
  so it can only over-estimate. `opsagent balance` reads the real bill, and the eval runner prints real spend.
- **Latency:** `span` events plus millisecond timings on every model and tool call. They feed "Where the time went"
  in the report, the console's Deep dive and the eval summary. Waits for retry back-off and hedge counts are recorded
  too.

### A16. Evaluation harness as a product feature
- **Scoring:** ground-truth checks on database state (not LLM judging), subgoal scores, WES+/WES− efficiency against
  counted reference trajectories (OSWorld-Human), and a verifier-agreement metric.
- **Conditions:** `clean`, `faults` (save fails once, relabelled UI, decoy toasts, session expiry, all at once), and
  `hard_web` (iframes, shadow DOM, confirm pop-ups).
- **Task sets:** a **held-out set** never used for tuning, and **3 runs per task**.
- **Parallelism:** `--parallel N` gives each worker its own isolated sandbox, on ports 8780 and up (B-9).

### A17. The sandbox company
- **Decision:** I built Kestrel Labs (mail plus a finance ERP in FastAPI and SQLite) with seeded traps:
  - phishing from a look-alike domain with prompt injection
  - a scanned-image invoice
  - a duplicate invoice
  - a vendor onboarding form
  - the newest Acme email is a *reminder*, and the invoice is in an older one
- **Why:** reproducible ground truth, injectable faults and no real-company data (a JD rule). Company knowledge is
  *data*: SOPs, `policy.yaml`, `systems.yaml`.

### A18. Operator console
- **Decision:** FastAPI plus vanilla JS. It has two levels:
  - **Quick view:** live Screen panel, progress track, checks, needed-a-human, evidence.
  - **Deep dive:** contract, every turn with reasoning, observations and policy, verification, memory, cost and
    latency.
- Approvals happen in the browser (amber band).
- **Why:** the user's job is to *trust and supervise*, not to read logs. Quick view answers "is it done and safe?",
  and Deep dive answers "why?".

### A19. Standing instructions: scheduled and event-triggered work
- **Decision:** a standing instruction is a request plus a trigger: every N minutes, daily at HH:MM, or an event a
  company system declares under `events:` in `systems.yaml` (for now `mail.new_message`). A small scheduler daemon
  (`opsagent scheduler`) fires due instructions. Each firing is an **ordinary run**, tagged
  `origin: standing:<id>`, so unattended work gets the same contract, policy, verification and report.
- **Security:** a trigger passes only **ids and times** into the request, never the email's subject or sender. That
  content is untrusted and could carry an injection into the trusted request text. The run reads the message itself,
  where it is wrapped as data.
- **Correctness:**
  - Runs of one instruction **never overlap**; the instruction fires again once the current run finishes.
  - Missed ticks aren't replayed; the next run sees the current state, and the duplicate check against the system of
    record catches anything already done.
  - A reset system is **resynced, not fired** (the event id went backwards).
  - State is one JSON file under a file lock, because the console and the daemon both edit it.
- **Long-running:** unattended runs that hit a policy approval park in the console queue until the Finance Manager
  decides. The first live test waited about 7 minutes for approval of a ₹55,224 payment, then carried on.
- **Rejected:** cron or Celery (more infrastructure than one JSON file and a loop), and putting email content in the
  trigger (an injection path).
- **Code:** `opsagent/scheduler.py`, `tests/test_scheduler.py` (5 tests, no LLM).
- **First live test:** a vendor email arrived and the run started 1 s later with nobody asking. It interpreted "new
  invoices" as *every unrecorded invoice*: it recorded 3 (the new one plus 2 older ones), flagged the look-alike-domain
  fraud email to the AP lead, and waited about 7 min for approval of the ₹55,224 payment. **Verified**, 51 steps,
  ≈ $0.23 real. Follow-up: the trigger now passes the new message ids, so the run knows which email arrived.

---

## Part B — Improvement timeline (problem found by a real run → fix → effect)

| # | Problem (how it showed up) | Fix | Effect / evidence |
|---|---|---|---|
| B-1 | The agent never saw its approval was granted, so it "confessed" to bypassing policy | Approval outcomes appear in the observation as notices | Approved runs complete cleanly |
| B-2 | Cost and cache: per-turn clearing would break the prompt-cache prefix | Batched tool-result clearing (60k chars, keep newest 3) + per-run cost cap | Cache hits stay high; runs about $0.08–0.10 off-peak |
| B-3 | The verifier couldn't open pages (sync Playwright allows one driver per thread) | Shared Chromium, separate read-only context for the verifier; verifier gets the audit log | Verifier works on every run |
| B-4 | Field values cut off at 80 chars, so the agent misread multi-line content | Limit raised to 600 with an explicit truncation marker | No more silent truncation |
| B-5 | Over-eager SOP use: it searched the mailbox for a bill that already existed in the ERP | Contract rule: scope to the request; start from the system of record | Fewer wasted steps (WES+ up) |
| B-6 | It asked the human for facts it could discover itself | Contract rule: open questions only for what only the requester knows | Fewer interruptions |
| B-7 | It re-asked a human after the human declined | Declines remembered in code; eval check `declined_not_reasked` | approval_declined under faults: 44 → 16 turns, $0.37 → $0.08 |
| B-8 | It invented its own "approval hunting" (looked for approval pages) | Told the agent and planner how approvals work (the platform asks at action time) | Behaviour gone |
| B-9 | Evals were slow when run one at a time | `--parallel N` with an isolated sandbox per worker | 4 tasks at once |
| B-10 | Reflection wrote a lesson that *relaxed* policy | Reflection reads the binding SOP/policy text; deterministic lesson ids; idempotent add | No policy-contradicting lessons |
| B-11 | Reflection added about 17 s to every run | Async reflection: file queue + worker pool, inline fallback | About 17.5 s saved per run; 4 workers = 4.3× throughput |
| B-12 | Mobile/console polish: flat type scale, horizontal scroll | 4-step type scale, `min-width: 0`, table overflow | Legible on recording and phone |
| B-13 | The verifier ran out of steps on multi-item tasks | Budget scales with criteria + forced final verdict | No more "no verdict" outcomes |
| B-14 | Held-out pass 1 scored 6/7: an approved multi-line email was blocked (browser CRLF vs FormData LF) | `_canon` normalises line endings before the payload match; regression test | Held-out 21/21 |
| B-15 | Real SaaS uses iframes, shadow DOM and pop-ups | Composed-DOM observer, per-frame rendering, cross-frame lookup, policy inside frames, dialog handling | `hard_web` 7/8 (5/6 → fix → 2/2) |
| B-16 | The contract read "enter invoice" as record-only, and the verifier passed it | The verifier audits the contract against SOPs (`SOP-n` criteria) | Contract gaps are caught |
| B-17 | The Flash contract read "unpaid" as a literal status, found none and reported none | Rule: interpret by meaning; an empty result from a guessed filter means look closer | Re-test 2/2 |
| B-18 | The contract phase was 40% of run time | Contract and reflection moved to the cheaper model (utility role) | Contract 87 s → 15 s; Globex task 460 s → 149 s; dev still 6/6 |
| B-19 | The account ran dry while the meter showed about $9.6; I assumed the meter under-counted 2× | Added a real balance check; **measured, and the theory was wrong** (off-peak meter matched the bill); meter now charges full price as a safety margin | Docs corrected; real spend measured per eval |
| B-20 | "Make it fast, not dumb": every call thought at full depth | Depth routing (A13). First tried `reasoning_effort=low`: **no effect** (82 s → 83 s). Then thinking off for light turns, after a 3-call probe found that the echo rule needs an empty `reasoning_content` | Operator model time 82 s → 67 s; 6/6 still pass, verifier 6/6 |
| B-21 | Provider tail latency: the 15 s contract call took 90–123 s in 4/6 runs; single turns took 65–156 s | Hedged requests (A14) | Contract mean 78 s → 26 s, worst 123 s → 46 s |
| B-22 | No visibility into where time went (timestamps only to the second) | Millisecond spans + per-call timings; latency breakdown in report, console and evals. **Also caught:** span data overwrote the event timestamp (`at`), renamed to `stage` | Breakdown: model calls ≈ 93% of wall time, browser ≈ 4% |

**Where the time goes (measured, 13 runs):** operator thinking 44%, verifier 39%, contract 10%, tools 4%,
reflection 3%. Speed therefore means fewer and shorter model calls; speeding up the browser wouldn't help.

---

## Part C — Ideas I tested and rejected (judgement, with evidence)

- **C1. Run graph / internal wiki / hot-cold tiered memory.** I designed it (the spec is kept as a decision record),
  then load-tested it at 100k runs, 1M facts and 50k lessons on an M2 (`docs/MEMORY_LOAD_TEST.md`):
  - an uncapped graph collapses
  - a capped graph is fastest but too narrow
  - capped two-tier gives the best quality
  - vector RAG (Qdrant + bge-small) is bound by embedding cost on CPU

  **Verdict:** too complex to maintain for the gain at this scale. I kept the simple lessons store behind an
  interface, so it can be swapped later.
- **C2. Memory-curator micro-agent.** I tested it next to async reflection (`docs/UPKEEP_AGENTS_TEST.md`). It leaked
  context between runs and *kept a policy-violating lesson*. **Rejected**; async reflection was adopted.
- **C3. Off-the-shelf memory (Mem0/Zep/Letta).** Rejected: our memory must carry provenance, ground itself in the
  SOPs and refuse untrusted writes. Those are governance features the frameworks don't model
  (`docs/DECISION_MEMORY.md`).
- **C4. Worker on the cheap model (`deepseek-fast`).** Each turn was faster, but it needed more turns, and the test
  was cut short by the balance running out. Not adopted until proven.
- **C5. Real ERP (Odoo) and MFA/TOTP automation.** Scoped out. Odoo setup cost too much time near the deadline, and
  automating MFA is the wrong thing for an agent to do. The right design is a human or SSO broker in the loop.
  Listed as next steps instead.
- **C6. Screenshots as the primary observation.** Rejected for cost and grounding error; vision stays a fallback.

---

## Part D — What I'd reverse or build next (founding-engineer framing)

1. **Exactly-once writes:** idempotency keys on every write, so resume can never duplicate.
2. **Verifier fast path:** judge from the record diff and the API first, and browse only for what those can't prove.
   It's the biggest remaining latency (about 65 s).
3. **Cross-model audit:** a verifier from a different model family (Claude worker, DeepSeek verifier, or the reverse).
   The adapter exists.
4. **Scale:** a queue of stateless workers with a browser pool. Runs are already event-sourced and resumable.
5. **Security:** signed, payload-bound approval tokens in Slack/Teams; policy in OPA/Cedar per tenant; a red-team eval
   set of injection pages and emails.
6. **Reach:** Gmail/Outlook APIs, ERPNext/Odoo, and desktop apps through OS accessibility APIs, with pixel-level
   computer use as the last rung of the ladder.
7. **Learning:** promote repeatedly verified workflows into reusable skills.
8. **Durable scheduling at scale:** move standing instructions from a JSON file to the job queue, add webhooks
   (Gmail push, ERP events) instead of polling, and per-instruction budgets and quiet hours.
