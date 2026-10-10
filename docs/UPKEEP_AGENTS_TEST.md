# Experiment: asynchronous memory-upkeep micro-agents

**Question:** should memory upkeep move off the run's critical path into concurrent background micro-agents, and
should a curator agent clean up lessons automatically? Today `Operator._complete` calls `opsagent.memory.reflect()`
inline at the end of every run, and nothing ever curates the lessons.

**Answer, in short:** **adopt the async Reflector**, because it's cheap, robust and parallel, and it removes 14–30 s
from every run. **Do not adopt the auto-merging Curator as built.** Its compression looks impressive (70 → 15
lessons), but manual review found harmful over-generalisation and contradictions it left unresolved.

Code: `experiments/upkeep_agents/` (`jobqueue.py`, `agents.py`, `run_test.py`). Raw results:
`experiments/upkeep_agents/results/upkeep-1010-0723.json`, `lessons_curated.jsonl`, `curation_ops.json`, `run_test.log`.
Data: the **14 real completed runs** in `runs/`, which are all of them at the time: 12 eval runs, `run2-acme`, and one
re-run. Model: DeepSeek V4 Pro (`MODEL_PROFILE=deepseek`), the same `reflect()` prompt and rules the product uses.

## Design tested

- **Queue:** jobs are JSON files in `pending/ → claimed/ → done/ | failed/`. A worker claims a job with an atomic
  `os.rename`, so exactly one worker wins. Job ids are deterministic (`reflect:<run_id>`), so enqueueing the same
  job twice is a no-op. A failed job goes back to `pending/` until it has made 3 attempts, then moves to `failed/`.
  A crashed worker's claim can be requeued (`requeue_stale`). (The module is named `jobqueue.py`, not `queue.py`:
  `queue.py` would shadow the standard-library `queue` that `concurrent.futures` imports.)
- **Workers:** a thread pool. The jobs are I/O-bound LLM calls, so threads parallelise them well despite the GIL.
- **Reflector micro-agent:** rebuilds the product's exact reflection inputs (contract render + compact trace) from
  `checkpoint.json` and `events.jsonl`, then calls `opsagent.memory.reflect()`. Lesson ids are
  `sha1(run_id + text)`, so a retry can't create duplicates.
- **Curator micro-agent:**
  - A **deterministic rule** retires guidance whose source run wasn't verified. It needs no LLM.
  - Concurrent LLM batches of up to 18 lessons, pre-grouped by token overlap, propose `merge` and `supersede`.
  - A cross-batch pass runs over the survivors.
  - Nothing is ever deleted: each lesson gets a status (`active`, `merged_into:<id>`, `superseded_by:<id>` or
    `retired`), and every operation is logged with a reason.

## Results

### 1. Critical-path saving: inline reflection today
Time from a run's final verdict to its inline `lessons` event, across all 14 runs:

| mean | median | max |
|---|---|---|
| **17.5 s** | 14 s | 30 s |

That's 5–10% of a typical 3–5 minute run that the user waits through and gets nothing for. In the experiment, a
reflect call itself took a median of 24–28 s (p95 32–60 s).

### 2. Throughput: 14 reflector jobs

| workers | wall time | speed-up | jobs/min | job p50 / p95 | cost |
|---|---|---|---|---|---|
| 1 | 424.9 s | 1.0× | 2.0 | 25.8 / 60.4 s | $0.072 |
| 4 | 98.8 s | **4.3×** | 8.5 | 24.3 / 31.9 s | $0.059 |
| 8 | 74.4 s | **5.7×** | 11.3 | 28.4 / 38.6 s | $0.066 |

Scaling is near-linear up to 4 workers. At 8 workers the speed-up is limited by the job count (14 jobs, with the
slowest jobs at the tail), not by the API: per-job latency barely moved and nothing was throttled. That works out to
about **$0.004–0.005 per run** for reflection.

### 3. Failure handling and idempotency of the queue
- **Injected failure:** the first attempt on `eval-1010-0612-r0-acme_invoice-faults` raised an error. The job went
  back to `pending/`, another worker completed it on attempt 2, and the error is recorded in the job file. That run
  produced **0 duplicate lesson ids**.
- **Re-enqueue after completion:** 0 of 14 jobs were accepted again in each configuration, as intended.

### 4. Curation quality
Input: 70 real lessons from the 8-worker pass, plus 2 clearly labelled **synthetic** lessons from a non-verified
run. The synthetic ones were added only to exercise the retirement rule, because all 14 real runs had passed
verification.

| metric | value |
|---|---|
| active lessons after curation | **17** of 72 (14 merged canonical lessons + 3 untouched originals) |
| operations | 30 merges, 0 supersessions, 2 retirements |
| real lessons absorbed into merges | 67 of 70 |
| synthetic unverified lessons retired by the rule | **2 of 2** ✔ |
| LLM calls / wall time / cost | 5 calls / **522 s** / **$0.195** |


**Good merges.** The paraphrases really were the same guidance:
- 2 → 1: "Acme Supplies Pvt Ltd is vendor V001 with email_domain acme-supplies.in" plus the same fact with the
  look-alike-domain detail, merged into one lesson that keeps both details.
- 2 → 1: two near-identical descriptions of the `/erp/vendors/new` form fields, merged with the "vendor ID appears in
  the URL" detail kept.
- 4 → 1: four runs' "compare the sender's domain exactly to the vendor's `email_domain` via `GET /vendors?q=`".

**Bad results, from manual review of all 17 active lessons:**
1. **Context leakage (wrong merge).** An 8-lesson merge folded one *approval-declined-specific* lesson ("searching the
   mailbox for 'Umbrella' may not find an invoice email when the bill already exists") into the general duplicate-check
   lessons. The result is a general rule: *"Before recording a vendor invoice, do not rely on searching the AP mailbox
   … (no invoice email will be found)"*. That's wrong for normal invoice processing and contradicts another active
   lesson: *"open the actual invoice email … essential"*.
2. **Contradictions left active.** One lesson says ≥ ₹50k invoices are "automatically routed for Finance Manager
   approval" when you schedule. Another, generalised from the declined run, says *"do not attempt scheduling before
   Finance Manager approval … request approval instead"*. Both stayed active. 0 supersessions were proposed.
3. **A policy-violating lesson survived.** An untouched original pitfall says *"do not send an additional 'suspected
   fraud' email about a look-alike domain … the extra message is unnecessary"*. That contradicts the finance-controls
   policy, which requires reporting suspected fraud. The curator never sees the SOPs or policy, so it can't catch this.
   (This lesson came from the product's own inline `reflect()`, so it is a finding about reflection too, not only
   about curation.)
4. **Kitchen-sink lessons.** The prompt's "two sentences at most" was ignored. Several canonical lessons merge 5–11
   sources into long paragraphs that mix distinct facts (API endpoints + bill page fields + mail GUI).
5. **Slow and costly relative to its value:** 522 s and $0.195 for 72 lessons. Each batch call produces a long JSON
   output with thinking enabled.

### 5. Idempotency of curation
A second curation pass over its own output proposed **0 merges, 0 supersessions and 0 retirements** (17 → 17 active).
It's stable, but the pass still cost **$0.034** for one LLM call that changed nothing. Curation should be incremental
(only new lessons against existing ones), not a full re-scan.

### 6. Cost
**$0.426 total over 48 LLM calls**: reflector $0.197 (42 calls), curator $0.195 (5 calls), idempotency pass $0.034
(1 call). That's **$0.026 over the $0.40 cap**. The budget check runs before each call, but up to 4 calls were in flight
at once and a call's cost is only known after it returns. A strict cap needs the guard to reserve an estimated cost
per in-flight call.

## Recommendation

**Adopt: the async Reflector.** It removes 14–30 s from every run's critical path, scales near-linearly with
workers, survives failures, and can't duplicate lessons. Minimal integration:
1. `Operator._complete` enqueues `reflect:<run_id>` instead of calling `reflect()` inline (the run report notes
   "lessons pending").
2. `opsagent upkeep --watch` runs a worker pool (default 4) over `runs/_queue/`.
3. `ExperienceMemory.add` uses the deterministic lesson ids from `lesson_id()`, so retries are no-ops.
4. Reserve-based budget guarding for concurrent calls.

**Do not adopt as built: the auto-merging Curator.** At our scale (tens of lessons) keyword recall tolerates
duplicates fine, and the observed failure modes (context leakage, contradictions, a policy-violating lesson kept
active) make automatic merging worse than leaving duplicates in place. If revisited:
- Run it **incrementally**, comparing each new lesson only with its nearest existing ones.
- **Flag duplicates instead of rewriting them**, or cap a merge at 3 members and keep each member's context.
- Give it the **SOPs and policy** so it can retire lessons that contradict authoritative documents.
- Add an explicit contradiction check across active lessons.
- Send merges through a **human review queue** before they become guidance.
- Recall then reads only `active` lessons.

**Separate finding for the product:** the inline `reflect()` produced a lesson that contradicts company policy (point
3 above). Reflection should get the relevant SOP and policy text and be told that lessons must not contradict them.
