# Decision: build our own memory layer (behind an interface) instead of Mem0 / Zep / Letta

## The problem we actually have
An AI employee needs three kinds of memory:
1. **Working memory** — facts found during this run (invoice no., amount, bill id). Must be exact, pinned, never "approximately recalled".
2. **Company knowledge** — SOPs, policies, which system does what. Authored by the company, versioned, small.
3. **Experience** — what worked / failed on past runs ("Acme invoices come from billing@acme-supplies.in", "ERP create-bill sometimes 502s; retry once"). Procedural, learned.

## Why not an established memory framework
1. **They optimize for a different benchmark.** Mem0, Zep, Letta etc. compete on LoCoMo / LongMemEval / BEAM —
   recalling facts from long *chat histories* (personalization). Our need is *procedural* memory for task execution;
   the relevant evidence is Agent Workflow Memory (+51% relative success on WebArena), LEGOMem (OfficeBench) and
   AF-TER (enterprise procedural skills, +3.7–6.7 pts per refinement round) — all store *workflows and lessons*, not chat facts.
2. **Exactness beats similarity for working memory.** An invoice amount must be retrieved exactly and pinned in context
   every turn, not surfaced by top-k vector search that may or may not return it. A structured notes store does this in ~50 lines.
3. **Company knowledge must stay authoritative and auditable.** SOPs/policies are source documents with owners and versions.
   Extracting them into an LLM-summarized fact store loses provenance and can silently mutate policy. We read the
   documents just-in-time (Anthropic context-engineering guidance) — at this corpus size a vector DB adds nothing.
4. **Provenance & trust are enterprise requirements.** Every lesson we store records which run produced it and whether that
   run's outcome was *verified*. Unverified experience is never promoted to guidance. Generic memory layers don't model
   "was this memory produced by a successful, verified run?"
5. **Security.** Memory written from untrusted content (e.g. a phishing email) is a persistence vector for prompt injection
   (memory poisoning). We only write lessons from verified runs, from our own reflection step, tagged with source — never raw environment text.
6. **Operational cost for this stack.** DeepSeek has no embeddings endpoint, so a framework would add a second model
   provider (and Zep/Graphiti a Neo4j instance) — more infra, keys, latency and failure modes for no measured gain.
7. **Debuggability.** We will be asked to explain and modify the system live. A 200-line memory module we own is fully
   explainable; a framework's extraction/merge heuristics are a black box exactly where agent bugs hide.
8. **We still measured it.** Memory is evaluated by ablation on our own task suite (memory off vs on, run 1 vs run 2),
   which measures what matters here instead of borrowing chat-recall scores.

## What we keep open
`opsagent/memory.py` exposes a small interface (`recall(task) -> list[Lesson]`, `record(run) -> list[Lesson]`).
Swapping in Mem0/Zep later (e.g. for multi-tenant scale or semantic recall over thousands of lessons) is an adapter, not a rewrite.
At production scale we'd add embeddings-based retrieval once lesson count makes keyword/tag retrieval insufficient — that's a
measured trigger, not a day-one dependency.
