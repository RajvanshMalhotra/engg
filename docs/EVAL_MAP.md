# Evaluation criteria → what in this repo proves it

Every feature must earn its place against one of CentrAlign's 8 criteria. If it doesn't map, we don't build it.

| Criterion | What we build | How we prove it (demo / eval) |
|---|---|---|
| **Autonomy** — decides next actions without being told each step | Goal → Task Contract (outcome, constraints, success criteria, subgoals) from a one-line request + JIT company SOP lookup; agent picks tools itself | One-line requests ("Process the Globex scanned invoice") complete end-to-end; trace shows the agent discovering steps from SOPs, not from the prompt |
| **Execution** — actually performs work | Real browser (Playwright) on a real sandbox ERP + mail app, real file/PDF/image reading, real HTTP API calls | Sandbox DB state changes (bills, vendors, sent mail); before/after state in the run report |
| **Reliability** — unexpected states, errors, retries | Structured tool errors, per-action state-delta check, retry/replan policy, session re-login, step budget + escalation, durable event log (resume after crash) | Fault-injection evals: `flaky_submit`, `layout_b` (UI drift), `toasts` (decoy changes), `session_expiry` — same tasks, pass with faults on |
| **Efficiency** (not a listed criterion, but asked in founding-eng interviews) | Grouped `fill_form`, batched context clearing, cost cap | OSWorld-Human WES+/WES− vs hand-written reference step counts per eval task; $/run and s/run |
| **Verification** — was the outcome actually achieved | Success criteria written *before* acting; `finish` is gated by an independent, clean-context, read-only verifier sub-agent that re-observes real state | Run report shows each criterion ✔/✘ with evidence; eval suite checks DB state programmatically and scores subgoals (OSWorld-Pro style) |
| **Generalization** — runtime unchanged across tasks | Task-agnostic runtime; company knowledge (SOPs, policy, system registry) is data in `company/`; tools are generic (browser, files, http, vision, human) | ≥5 different tasks (invoice PDF, invoice scan, vendor onboarding, ambiguity, injection) with zero code changes; adding a task = adding a SOP |
| **Engineering quality** | Small modules with clear seams (llm, contract, runtime, tools, policy, verifier, memory, store), typed dataclasses, tests, no framework magic | Unit tests for policy/browser/memory; eval runner; readable 1-file core loop |
| **Product thinking** — user's actual objective | Contract captures intent + implicit company requirements (e.g. dedupe, approvals, notify AP lead); asks only when genuinely ambiguous; human-readable run report with evidence | "Latest Acme email" is a reminder → agent processes the actual invoice, notices nothing new is needed / or that it's already recorded; summary written for an AP manager |
| **Technical understanding** | `docs/` explains each decision with the research behind it (ASIL, OSWorld-MCP/2.0/Pro, Desktop-Delta, AWM, Anthropic context engineering) | README "Design decisions" + "Why not X" sections |

## Founding-engineer extras (interview emphasis: architecture, scalability, reliability, security, extensibility)
- **Security:** policy enforced at the network layer (every non-GET request from browser or API tool passes the policy engine), secrets never enter the LLM context (`fill_secret`), environment content tagged untrusted (prompt-injection demo), verifier is read-only by construction, segregation of duties (agent can't approve vendors).
- **Extensibility:** new system = entry in `company/systems.yaml` (+ optional API); new rule = entry in `company/policy.yaml`; new procedure = markdown SOP; new model = adapter.
- **Scalability path:** runs are durable event logs keyed by run id → maps directly onto a task queue + workers; documented in README "Path to production".
