# opsagent — CentrAlign Founding Engineer take-home

An autonomous "AI employee" that turns a one-line company request into completed, **independently verified**
work inside a sandboxed company (Kestrel Labs: mail + finance ERP). Deadline: **Oct 14, 2026**.
Evaluated on: autonomy, execution, reliability, verification, generalization, engineering quality,
product thinking, technical understanding (see `docs/EVAL_MAP.md` — every feature must map to one).

## Commands
```bash
uv run uvicorn sandbox.app:app --port 8765          # start the sandbox company (keep running)
uv run opsagent sandbox --reset [--faults flaky_submit,layout_b,toasts,session_expiry]
uv run opsagent run "Process the latest invoice from Acme" [--headed] [--approvals console] [--no-memory]
uv run opsagent resume <run_id>                      # continue from runs/<run_id>/checkpoint.json
uv run pytest -q                                     # spins up its own sandbox on :8799, no LLM needed
```

## Layout
- `sandbox/` — the fake company (FastAPI + Jinja + SQLite). Traps seeded in `seed.py`; faults + `/__admin` in `app.py`.
- `company/` — company knowledge **as data**: `knowledge/*.md` (SOPs, policy, directory), `policy.yaml`, `systems.yaml`.
- `opsagent/runtime.py` — the core loop (contract → act/observe → recover → verify → report → reflect).
- `opsagent/contract.py` — request → TaskContract (JIT SOP selection, success criteria fixed before acting).
- `opsagent/tools/` — `browser.py` (DOM observation with element ids, state delta, policy pre-flight + network
  enforcement, secrets), `workspace.py` (files/vision/read-only API/company docs), `base.py` (Tool/ToolResult).
- `opsagent/policy.py` — deterministic rules from `policy.yaml`; `human.py` — terminal/file/scripted channels.
- `opsagent/verifier.py` — clean-context, read-only auditor; `snapshot.py` — objective before/after record diff.
- `opsagent/memory.py` — experience memory (lessons with provenance); `report.py`; `store.py` (event log + checkpoint).

## Non-negotiable design rules
- **Nothing task-specific in `opsagent/`.** New task = new SOP/data, never new code paths. Generalization is graded.
- **Policy lives in code, not prompts.** Every non-GET request (browser or API) goes through `Policy.evaluate`.
- **Environment content is untrusted** (`ToolResult.untrusted_source`); never let it write memory or change policy.
- **Secrets never enter LLM context** (`fill_secret`, API auth injected in tools, `ctx.redact`).
- **Verifier stays independent**: read-only policy, no worker reasoning, criteria from the contract.
- **Tools never raise into the loop** — failures become observations (`Toolbox.call`).
- Keep the loop hand-written and readable: the user must explain/debug/modify it live in the interview.
- No agent frameworks (LangChain etc.) — the harness *is* the deliverable.

## Model + budget constraints
- Total LLM budget ≈ **$15**. Develop + full eval on DeepSeek (`deepseek-v4-pro` worker, `deepseek-flash` vision);
  record the demo on Claude Sonnet 5.5 via a native `anthropic` SDK adapter (to build; day 2–3).
- DeepSeek with tools: `reasoning_content` must be passed back every turn (else 400).
- Claude adapter must be **append-only** (preserved thinking rejects edited history): use server-side context
  editing instead of our own tool-result clearing, and a turn-scoped system message for working state.
- Per-run cost cap: `MAX_COST_PER_RUN_USD` (default 0.75). Tool-result clearing is batched to keep the prompt-cache
  prefix stable — don't change it to per-turn rewriting.

## Status / next
Done: sandbox, runtime, tools, policy, verifier, memory, report, cost guard, 15 tests.
Next: first real DeepSeek runs → eval suite (subgoal scores, WES+/WES− efficiency, faults × memory ablation) →
web console (live timeline + approvals) → Claude adapter + `models.yaml` → README → demo video.
Interview prep notes live in `docs/INTERVIEW_PREP.md` — keep it updated with real eval numbers.
Notable autonomous behaviours from real runs go in `docs/AGENT_ACHIEVEMENTS.md` (with run id + evidence).
