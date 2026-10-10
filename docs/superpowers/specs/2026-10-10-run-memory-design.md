# Run memory: inspector, knowledge wiki, two-tier recall — design

Status: draft for review · Date: 2026-10-10 · Deadline context: submission Oct 14

## 1. Purpose

Every agent run already leaves an append-only event log (`runs/<id>/events.jsonl`), a checkpoint, a report and
evidence files. Today those are only readable by grepping. This design turns them into three things:

1. **Deep scrutiny for humans** — a per-run inspector to explain, debug and recalibrate the agent (interview: "explain
   why the agent made a decision", "debug an issue").
2. **Context and proof for humans** — a knowledge wiki: "show me everything about Acme" across all runs, each fact
   linked to the raw event that proves it.
3. **Memory for the agent** — a two-tier recall: a small, fast, cache-friendly *local* tier used every run, and a
   *deep*, auditable tier searched on demand.

And it settles one architectural question with evidence instead of opinion: **does graph-based retrieval scale for a
large company, or is a two-tier (hot set + full-text index) design better?** — via a synthetic benchmark at $0.

## 2. Principle

**The event log is the single source of truth. Everything else is a projection that can be rebuilt from it.**
The inspector, the wiki, the graph view, the deep index and the hot tier are all derived read models. Every derived
fact carries a pointer `(run_id, event_index)` back to the raw event. Nothing is extracted by an LLM, so derivation is
deterministic and usable as proof.

## 3. Scope

In scope (in build order, each time-boxed):

| # | Piece | Time box | Changes agent behaviour? |
|---|---|---|---|
| A | Run inspector ("Deep dive" tab on the run page) | 1.5 h | no |
| B | Fact projection + deep index (SQLite FTS5) + knowledge wiki pages | 2.5 h | no |
| C | Synthetic scale benchmark (keyword vs graph vs deep index vs two-tier) | 2 h | no |
| D | Two-tier recall in the agent behind `MEMORY_MODE` flag + real-agent ablation | 1.5 h | only when flag is on |

Out of scope: LLM entity extraction, a graph database, embeddings, multi-tenant storage, editing or deleting runs.

Cut line: if time runs short, D ships as the benchmark result + README "next step"; A–C still ship.

## 4. Components

### A. Run inspector
A "Quick view / Deep dive" toggle on the existing run page. Deep dive shows, per turn: the agent's visible text and
reasoning (as logged), each tool call with full arguments and the stored observation (first 3,000 chars), policy
decisions and approvals inline, cost delta for the turn, and evidence thumbnails. Below the turns: the full contract
(including amendments), lessons recalled at start, the verifier's transcript and verdict with per-criterion evidence,
the objective record diff, and lessons produced. Data source: existing `/api/runs/{id}` (events + checkpoint); no new
storage. Turn grouping uses the `step` field already on `llm` and `tool` events.

### B. Fact projection, deep index, knowledge wiki
`opsagent/runindex.py`:
- `extract(run_dir) -> list[Fact]` — deterministic. A `Fact` is `(subject, relation, object, run_id, event_idx, at,
  text)`. Sources: `start`/`complete` (run, request, status), `contract.relevant_docs` and `read_company_doc` (run →
  reads → SOP), `api_get` paths (run → looked_up → vendor/bill id), `open_url` `/mail/m/<n>` (run → opened → email),
  `download` (run → read → document), `policy`/`approval` (run → requested/approved/declined → action, by → person),
  POST `/mail/send` fields (run → emailed → person), `verify_start.state_changes` CREATED/UPDATED lines
  (run → created/updated → bill/vendor), `lessons` (run → learned → lesson).
- Entity ids are normalized strings: `vendor:V001`, `bill:BILL-1004`, `email:3`, `person:priya.nair@kestrel.in`,
  `sop:sop_invoice_processing`, `lesson:<id>`, `run:<id>`. Display names come from the same events (e.g. vendor name
  from API results) — no lookups into live systems, so the projection describes what the agent saw at the time.
- `RunIndex` (SQLite file `runs/index.db`): tables `facts` and `runs_indexed(run_id, last_event_idx)`; FTS5 table
  over `text` for deep search. **Incremental**: indexing reads only events after `last_event_idx` per run, so update
  cost is proportional to new events, not total history. A `rebuild()` exists and must produce identical rows.
- Console: a "Knowledge" page with a search box (FTS5) and lists by entity type; an entity page shows its facts
  grouped by relation, newest first, each linking to the run inspector at that event, plus a small 1-hop neighbourhood
  diagram (SVG, hub nodes like "ERP" collapsed into counts).

### C. Synthetic scale benchmark — `evals/memory_bench.py`
Generates a large company deterministically (seeded): N runs ∈ {1k, 10k, 100k}, ~V vendors (power-law usage), every
run touching shared hubs (ERP, invoice SOP), and L lessons. Ground truth is planted: for each of Q test queries
("pay Vendor-0423's invoice", "vendor bank change from look-alike domain", …) the relevant lessons/runs are known.
Retrieval designs compared:
1. **Keyword scan** — current `ExperienceMemory.recall` (linear scan over lessons).
2. **Graph** — in-memory adjacency built from the same facts; query entities → lessons within 1 hop and 2 hops;
   variants with and without hub capping (ignore nodes above a degree threshold).
3. **Deep index** — FTS5/BM25 over fact and lesson text.
4. **Two-tier** — hot set (bounded: top-K verified lessons by recency × usage, plus per-entity one-line summaries for
   the most active entities, capped at a token budget) answered first; on a miss for a named entity, fall back to
   the deep index.

Measured per design × N: build time, incremental update time (add 1 run), memory (RSS delta), query latency p50/p95,
recall@5 against planted ground truth, and for two-tier the hot-tier size in tokens (must fit the cached prompt
prefix budget, default 2,000 tokens). Output: `evals/results/memory-bench-<ts>.json` + a markdown table and one chart
in `docs/MEMORY_BENCH.md`.

### D. Two-tier recall in the agent
`MEMORY_MODE` ∈ `off | keyword (default today) | two_tier`. In `two_tier`:
- **Local tier** is rendered into the *system prompt* (stable within a run → stays in the cached prefix): the hot set
  from §C for the request's linked entities (entity linking = exact match of vendor names/ids, SOP names and system
  names already present in the index).
- **Deep tier** is a read-only tool `search_history(query)` returning matching facts with run/event citations, for
  when the agent needs specifics (e.g. "previous bank-detail changes for Acme").
- Lessons still come only from our reflection step (unchanged anti-poisoning rule); facts come only from events.

Real-agent ablation: 2–3 tasks × {off, keyword, two_tier}, sequenced so memory has prior runs to draw on; report
turns, cost, success. Expected honest outcome: small differences at our scale; the benchmark carries the scaling
argument.

## 5. Interfaces

```python
# opsagent/runindex.py
@dataclass
class Fact: subject: str; relation: str; object: str; run_id: str; event_idx: int; at: str; text: str
def extract(run_dir: Path) -> list[Fact]
class RunIndex:
    def __init__(self, path: Path)
    def update(self, runs_dir: Path) -> int            # incremental; returns facts added
    def rebuild(self, runs_dir: Path) -> int
    def search(self, query: str, k: int = 10) -> list[Fact]   # deep tier (FTS5)
    def entity(self, entity_id: str) -> list[Fact]
    def neighbours(self, entity_id: str, max_degree: int = 200) -> dict[str, int]
    def hot_set(self, entities: list[str], token_budget: int = 2000) -> str   # local tier text
```
Console API: `GET /api/knowledge/search?q=`, `GET /api/knowledge/entity/{id}`; run page reuses `/api/runs/{id}`.

## 6. Testing

- `extract` on a fixture run dir → expected facts (incl. event_idx provenance).
- `update` twice is idempotent; `update` after appending events adds only new facts; `rebuild` equals incremental.
- FTS5 search finds a planted fact; entity page groups by relation; hub capping excludes high-degree nodes.
- Benchmark runs at N=1k in CI-size time (< 30 s) with fixed seed; recall@5 ≥ 0.9 for at least one design (sanity).
- Two-tier hot set never exceeds its token budget.
- Console smoke: inspector and knowledge pages render for an existing run (Playwright screenshot check).

## 7. Success criteria

- In the interview: from any run, one click to see why each action happened (inspector); from "Acme", one click to
  every run, bill, approval, email and lesson tied to it, each linked to the raw event.
- A benchmark table that answers: at 100k runs, which design gives the best recall@5 within p95 < 50 ms, and does the
  hot tier fit the cached prefix budget.
- No regression: existing 21 tests pass; agent behaviour unchanged unless `MEMORY_MODE=two_tier`.

## 8. Risks

- **Schedule** — mitigated by build order and the cut line (§3).
- **Synthetic data flatters a design** — mitigated by power-law usage, real hubs, and reporting all designs on the
  same data; we state clearly it is synthetic.
- **Event schema drift** — extraction ignores unknown event kinds; extraction tests pin the schema.
