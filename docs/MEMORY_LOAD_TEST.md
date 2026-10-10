# Memory architecture load test

**Question.** Is graph-based memory too slow or unsuitable at production scale, and is a capped two-tier design
(write-time hot set + cold index) better? Vector RAG on a real vector database was added as a third family.
**Answer in one line:** a *capped* graph is the fastest thing we measured, but it can only answer entity-keyed questions;
an *uncapped* graph degrades exactly as hypothesised; vector RAG is bottlenecked by CPU embedding, not by the vector DB;
the capped two-tier design gives the best quality/latency/cost balance for agent memory.

Code: [`experiments/memory_load/`](../experiments/memory_load) · raw results: `experiments/memory_load/results/` ·
reproduce: `experiments/memory_load/run_all.sh` · cost: $0 (no LLM calls).

## Setup

**Machine (laptop numbers):** Apple M2, 8 cores, 8 GB RAM, macOS 26.5.1, Python 3.13.5. Every server and client ran on
this one machine.
**Engines:** SQLite 3.45.3 FTS5 (WAL), Qdrant **1.19.2** server binary on localhost (gRPC), embeddings with fastembed
`BAAI/bge-small-en-v1.5` (384-d, ONNX on CPU, no torch).

**Synthetic company (seeded, deterministic, generated across 8 processes in 0.6 s):**
- 100,000 runs; 20,000 vendors with Zipf(1.1) usage; **1,000,000 facts** (run → vendor, bill, email, 2 people,
  SOP, invoice, plus the **hubs `system:erp`, `system:mail`, `sop:invoice_processing` that every run touches**).
- **50,326 lessons**, of which 15,086 are vendor-specific (half name the vendor in text, half are linked *only* by
  provenance, i.e. learned in a run that touched the vendor) and the rest are generic. Generic lessons cover 40 topics,
  written from 6 sentence frames with sampled synonyms.
- **500 planted queries:**
  - 250 vendor-scoped: "Process the latest invoice from *X*". The truth is X's vendor-specific lessons, 2–40 each.
  - 250 paraphrased topic questions, written from *different* frames and synonym draws than the lessons. The truth is
    that topic's generic lessons.
- A 10,000-run company was generated the same way for the scale sweep.

**Architectures and their caps:**

| Arm | What it is | Caps |
|---|---|---|
| keyword | token-overlap scan over all lessons (today's `ExperienceMemory.recall`) | top-k |
| graph | in-memory adjacency entity→runs→lessons; exact-name entity linking | **hub cap**: never expand nodes with degree > 1,000; **fan-out cap**: ≤ 200 most-recent runs per entity |
| graph_uncapped | same graph, no caps | none (shows the failure mode) |
| two_tier | **hot**: per-entity list of the 5 newest lessons, maintained *at write time*, only for recurring entity types (vendor, person), hubs excluded; **cold**: SQLite FTS5/BM25 over lessons | 5 per entity, hub cap 1,000, type cap |
| vector | Qdrant collection, cosine, **HNSW m=16, ef_construct=128, hnsw_ef=64**, indexing_threshold=1,000 KB (fully indexed before timing: 50,326/50,326 vectors); query and write-path embedding with bge-small | top-k |
| hybrid | two-tier hot set + Qdrant as the cold tier | as above |

**Load harness:**
- Each arm runs in a fresh process, one arm at a time, so arms don't compete for CPU.
- Open-loop readers: latency is measured from the *scheduled* send time, so queueing counts.
- Paced writers append new runs and lessons.
- A probe writes a lesson for a brand-new vendor every second and polls until a query returns it (**write→read lag**,
  which includes the write path itself).
- Stages run for 15 s each: (reads/s, writes/s) = (10, 5) → (50, 20) → (200, 20) → (1000, 50). An arm stops after the
  first stage where achieved reads fall below 70% of target or p95 exceeds 1 s.
- The 10k sweep ran four arms in parallel, in fresh processes, because it measures quality and size only.
- Embedding and data generation were parallelised across processes.

## Results

### Quality and no-load cost at 100k runs

| Arm | Vendor recall@5 | Paraphrase P@5 | Query p50 / p95 (no load) | Build | Memory |
|---|---|---|---|---|---|
| keyword | 0.46 | 0.68 | 10.0 / 12.6 ms | 0.2 s | +61 MB |
| **graph (capped)** | 0.70 | **0.00** | **0.01 / 0.03 ms** | 0.1 s | +54 MB (1 M edges) |
| graph_uncapped | **0.00** | 0.01 | 23.0 / 28.4 ms | 0.2 s | +59 MB |
| **two_tier** | **0.70** | **0.84** | 2.9 / 7.2 ms | 0.5 s | +21 MB + 19 MB FTS file |
| vector (Qdrant) | 0.04 | 0.80 | 10.3 / 26.0 ms | 6.3 s load + **368 s embedding** | Qdrant server 121–167 MB |
| hybrid | 0.70 | 0.80 | 9.5 / 28.9 ms | 6.7 s + embedding | Qdrant server 124–255 MB |

Two-tier hot set per query: **p50 ≈ 30 tokens, max 129 tokens**. Small enough to sit in a cached prompt prefix.

### Scale sweep, 10k → 100k runs (10× more history)

| Arm | Vendor recall@5 | Paraphrase P@5 | Query p95 |
|---|---|---|---|
| keyword | 0.58 → 0.46 | 0.70 → 0.68 | 1.3 → 13.2 ms (linear scan) |
| graph (capped) | 0.68 → 0.70 | 0 → 0 | 0.02 → 0.06 ms |
| graph_uncapped | 0.02 → **0.00** | 0.00 → 0.01 | 2.5 → **28.4 ms (11×)** |
| two_tier | 0.68 → 0.70 | 0.78 → 0.84 | 0.8 → 6.8 ms |
| vector | 0.19 → 0.05 | 0.82 → 0.80 | 18.4 → 18.4 ms (embedding-bound, flat) |
| hybrid | 0.68 → 0.70 | 0.82 → 0.80 | 8.8 → 13.6 ms |

### Under concurrent read + write load, 100k runs

| Arm | Highest stage sustained | Read p50 / p95 / p99 at that stage | Write→read lag p50 / p95 | First stage that saturated |
|---|---|---|---|---|
| keyword | 50 r/s + 20 w/s | 14.6 / 18.0 / 37.0 ms | 12.7 / 48.8 ms | 200 r/s: achieved 11 r/s, p95 12.1 s |
| **graph (capped)** | **1000 r/s + 50 w/s** | **0.3 / 0.6 / 3.4 ms** | **0.06 / 0.09 ms** | none |
| two_tier (threads) | 200 r/s + 20 w/s | 4.3 / 9.6 / 15.3 ms | 10.5 / 18.1 ms (SQLite commit) | 1000 r/s: achieved 171 r/s |
| two_tier cold tier, **reader processes** | 1,123 r/s with 8 processes while one writer commits 20/s | 6.3 / 13.9 / 19.7 ms | — | (1 process: 222 r/s; 4: 843 r/s) |
| vector (Qdrant) | 50 r/s + 20 w/s | 23.3 / 110 / 165 ms | 22.8 / 178 ms, plus 3 probe misses per stage | 200 r/s: achieved 10 r/s, p95 8.5 s |
| hybrid | 50 r/s + 20 w/s | 12.5 / 19.0 / 24.5 ms | 22.0 / 26.4 ms | 200 r/s: achieved 40 r/s, p95 4.0 s |

**Embedding cost on this laptop:**
- Bulk: 88 texts/s in one process; 137 texts/s with 4 processes (the best setting); 23 texts/s with 8 processes,
  where 8 GB of RAM ran out.
- On the write path: 10–18 ms per lesson when idle, with p95 above 540 ms under read load.
- That makes **embedding the bottleneck, not Qdrant**: at saturation the writers fell from 20 w/s to 3 w/s.

## Verdict

1. **"Graph memory is too slow at production scale" is *refuted* for a capped graph and *confirmed* for an uncapped one.**
   - With caps, the graph was the fastest arm by an order of magnitude: 1000 r/s at p95 0.6 ms, sub-0.1 ms write→read
     lag, 54 MB for 1 M edges.
   - Without caps, hub nodes (ERP, mail, the invoice SOP) pull the whole history into every query. Latency grows
     linearly with history (11× for 10× data) and recall collapses to 0.00 because the hubs drown the signal.
   - **Caps are a correctness requirement, not an optimisation.**
2. **The graph's real limitation is coverage, not speed.** An entity graph only answers questions keyed by an entity it
   can link (paraphrase P@5 = 0.00). Any graph memory still needs a text or vector index beside it.
3. **Capped two-tier is the best balance for agent memory.**
   - It was best or tied-best on both query types (vendor recall 0.70, paraphrase P@5 0.84 here).
   - It used the least memory and kept the hot set to ≤ 129 tokens, so it fits the prompt cache.
   - It sustains 200 r/s from Python threads, and its cold tier scales out with processes or read replicas
     (1,123 r/s on 8 local processes).
   - The graph work happens **once, at write time**, bounded by caps, instead of on every read.
4. **With a real vector database, the database is not the bottleneck; the embedding model on CPU is.**
   - Qdrant held 50k vectors in an HNSW index at 120–250 MB.
   - Embedding cost ~10 ms per query and per write, which saturated this laptop around 50 r/s and added ~20 ms of
     write→read lag.
   - Pure vector search can't resolve exact entity names (vendor recall 0.04). Hybrid fixes that (0.70) but inherits
     the embedding cost.
   - On this synthetic data, vector paraphrase quality (0.80) did **not** beat BM25 (0.84). See the caveats.

**Recommendation:**
- **Agent memory:** keep today's simple lesson memory while lessons number in the tens; at that size every arm is
  equivalent, and that is why the product doesn't ship any of this yet. If lesson volume grows, upgrade to capped
  two-tier (entity-keyed hot set maintained at write time + BM25 cold). Add a vector cold tier (hybrid) only when lessons
  are genuinely free-text and paraphrastic, *and* embeddings run on batched or GPU serving. Never run uncapped graph
  traversal on the read path.
- **Human audit search:** BM25/FTS over the run event log (exact, cheap, scales with read processes or replicas). Use
  graph views only as 1-hop neighbourhoods with hub caps.

## Caveats

- **Synthetic data.** Lessons and queries come from templates with a limited synonym vocabulary, which favours lexical
  search (BM25). On real free-text lessons, embeddings would likely gain relative to BM25. Vendor names reuse shared
  words, which hurts keyword and vector arms on vendor queries.
- **One laptop, everything co-located.** Qdrant, the embedding model, the load generator and the arms share 8 cores
  and 8 GB. Server hardware, GPU embedding or a separate Qdrant host would raise the vector arms' ceilings
  substantially. These are laptop numbers.
- **Python GIL.** Thread-based numbers are pessimistic for the CPU-bound arms (keyword, FTS). The multi-process FTS row
  shows the headroom. Under-load latency at 10 r/s is *higher* than at 1000 r/s for the graph; that's a harness floor
  (thread wake-up plus the lag probe's polling), not backend cost.
- Write→read lag includes the write call itself. Vector probe misses mean the brand-new vendor's lesson was not in the
  top 5 within 3 s.
- Memory deltas come from fresh processes. Qdrant memory is the server's own RSS.
- Stages are short (15 s). Qdrant segments created during the run stay unindexed until its optimizer merges them, as
  they would in production.
