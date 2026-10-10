"""Five memory backends behind one interface, each with explicit caps.

    build(data)                               bulk load the synthetic company
    add_run(run_id, vendor, ents, lessons)    concurrent write path (thread-safe)
    query(text, k) -> [lesson ids]            concurrent read path
    link_vendor(name, vid)                    make a new vendor name linkable (write path)
"""

import os
import re
import sqlite3
import threading
from collections import defaultdict, deque


STOP = set("a an the of to for and or in on at by with from is are be this that it as into via any all when if "
           "what how do we should i this for".split())
HUB_TYPES = ("system:", "sop:")
HOT_TYPES = ("vendor:", "person:")   # type cap: hot entries only for recurring entities (not one-off bills/emails)


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower()) if w not in STOP and len(w) > 2]


class Linker:
    """Exact-name entity linking (longest match) — the 'graph' side's entry point."""

    def __init__(self):
        self.by_name: dict[str, int] = {}
        self.lock = threading.Lock()

    def add(self, name: str, vid: int) -> None:
        with self.lock:
            self.by_name[name.lower()] = vid

    def link(self, text: str) -> list[str]:
        words = re.findall(r"[a-z0-9\-]+", text.lower())
        for n in (5, 4, 3, 2):  # longest span first: "Silver Gate Electricals 2" beats "Silver Gate Electricals"
            for i in range(len(words) - n + 1):
                vid = self.by_name.get(" ".join(words[i:i + n]))
                if vid is not None:
                    out = [f"vendor:{vid}"]
                    if "invoice" in words:
                        out.append("sop:invoice_processing")   # realistic: requests also mention hub concepts
                    return out
        return ["sop:invoice_processing"] if "invoice" in words else []


# ---------------------------------------------------------------------------------------------- a. keyword scan
class KeywordScan:
    name = "keyword"

    def __init__(self, **_):
        self.ids: list[str] = []
        self.toks: list[frozenset] = []
        self.linker = Linker()

    def build(self, data):
        for l in data["lessons"]:
            self.ids.append(l["id"])
            self.toks.append(frozenset(tokens(l["text"])))
        for vid, name in enumerate(data["vendor_names"]):
            self.linker.add(name, vid)

    def link_vendor(self, name, vid):
        self.linker.add(name, vid)

    def add_run(self, run_id, vendor, ents, lessons):
        for l in lessons:   # append-only lists; CPython list.append is atomic
            self.toks.append(frozenset(tokens(l["text"])))
            self.ids.append(l["id"])

    def query(self, text, k=5):
        q = set(tokens(text))
        n = min(len(self.ids), len(self.toks))
        scored = [(len(q & self.toks[i]), i) for i in range(n)]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [self.ids[i] for _, i in scored[:k]]

    def stats(self):
        return {}


# ---------------------------------------------------------------------------------------------- b. graph
class Graph:
    """Entity graph: entity -> runs (recency order) -> lessons. Caps: never expand hubs (degree > hub_cap);
    visit at most fanout_cap most-recent runs per entity. capped=False shows what happens without caps."""

    def __init__(self, hub_cap=1000, fanout_cap=200, capped=True, **_):
        self.name = "graph" if capped else "graph_uncapped"
        self.hub_cap, self.fanout_cap, self.capped = hub_cap, fanout_cap, capped
        self.runs_of: dict[str, list[str]] = defaultdict(list)
        self.lessons_of: dict[str, list[str]] = defaultdict(list)
        self.linker = Linker()

    def build(self, data):
        for run, ent in data["facts"]:
            self.runs_of[ent].append(run)
        for l in data["lessons"]:
            self.lessons_of[l["run"]].append(l["id"])
        for vid, name in enumerate(data["vendor_names"]):
            self.linker.add(name, vid)

    def link_vendor(self, name, vid):
        self.linker.add(name, vid)

    def add_run(self, run_id, vendor, ents, lessons):
        for l in lessons:
            self.lessons_of[run_id].append(l["id"])
        for e in ents:
            self.runs_of[e].append(run_id)

    def query(self, text, k=5):
        score: dict[str, float] = defaultdict(float)
        for ent in self.linker.link(text):
            runs = self.runs_of.get(ent, [])
            if self.capped:
                if len(runs) > self.hub_cap:
                    continue
                runs = runs[-self.fanout_cap:]
            for rank, run in enumerate(reversed(runs)):
                for lid in self.lessons_of.get(run, ()):
                    score[lid] += 1.0 / (1 + rank * 0.01)
        return [lid for lid, _ in sorted(score.items(), key=lambda x: -x[1])[:k]]

    def stats(self):
        return {"entities": len(self.runs_of)}


# ---------------------------------------------------------------------------------------------- cold: FTS5
class FTSCold:
    """SQLite FTS5 (WAL): one writer connection, one connection per reader thread."""

    def __init__(self, path):
        self.path = path
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        self.w = sqlite3.connect(path, check_same_thread=False)
        self.w.execute("PRAGMA journal_mode=WAL")
        self.w.execute("PRAGMA synchronous=NORMAL")
        self.w.execute("CREATE VIRTUAL TABLE lessons USING fts5(lid UNINDEXED, text)")
        self.wlock = threading.Lock()
        self.local = threading.local()

    def bulk(self, rows):
        with self.wlock:
            self.w.executemany("INSERT INTO lessons (lid, text) VALUES (?, ?)", rows)
            self.w.commit()

    def add(self, rows):
        with self.wlock:
            self.w.executemany("INSERT INTO lessons (lid, text) VALUES (?, ?)", rows)
            self.w.commit()   # committed => visible to readers (write->read lag measured on this)

    def _conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self.local.c = c
        return c

    def search(self, text, k):
        toks = tokens(text)
        if not toks:
            return []
        match = " OR ".join(f'"{t}"' for t in toks)
        return [r[0] for r in self._conn().execute(
            "SELECT lid FROM lessons WHERE lessons MATCH ? ORDER BY bm25(lessons) LIMIT ?", (match, k))]


# ---------------------------------------------------------------------------------------------- hot tier
class Hot:
    """Per-entity bounded list of recent lessons, maintained at WRITE time (the graph work is done once, when a
    lesson is produced, for the entities its run touched — never at read time). Hubs are skipped."""

    def __init__(self, per_entity=5, hub_cap=1000):
        self.per_entity, self.hub_cap = per_entity, hub_cap
        self.entries: dict[str, deque] = {}
        self.degree: dict[str, int] = defaultdict(int)
        self.text: dict[str, str] = {}

    def add(self, ents, lessons):
        for e in ents:
            self.degree[e] += 1
        if not lessons:
            return
        for e in ents:
            if not e.startswith(HOT_TYPES) or self.degree[e] > self.hub_cap:
                continue
            d = self.entries.get(e)
            if d is None:
                d = self.entries[e] = deque(maxlen=self.per_entity)
            for l in lessons:
                d.append(l["id"])
        for l in lessons:
            self.text[l["id"]] = l["text"]

    def get(self, ents):
        out = []
        for e in ents:
            out += list(reversed(self.entries.get(e, ())))
        return out

    def tokens_for(self, ids):
        return sum(len(self.text.get(i, "")) for i in ids) // 4


# ---------------------------------------------------------------------------------------------- c. two-tier
class TwoTier:
    name = "two_tier"

    def __init__(self, workdir=".", per_entity=5, hub_cap=1000, **_):
        self.hot = Hot(per_entity, hub_cap)
        self.cold = FTSCold(os.path.join(workdir, "two_tier.db"))
        self.linker = Linker()
        self.hot_tokens: list[int] = []

    def build(self, data):
        ents_by_run = defaultdict(list)
        for run, ent in data["facts"]:
            ents_by_run[run].append(ent)
        lessons_by_run = defaultdict(list)
        for l in data["lessons"]:
            lessons_by_run[l["run"]].append(l)
        for run, _ in data["runs"]:
            self.hot.add(ents_by_run[run], lessons_by_run.get(run, []))
        self.cold.bulk([(l["id"], l["text"]) for l in data["lessons"]])
        for vid, name in enumerate(data["vendor_names"]):
            self.linker.add(name, vid)

    def link_vendor(self, name, vid):
        self.linker.add(name, vid)

    def add_run(self, run_id, vendor, ents, lessons):
        self.hot.add(ents, lessons)
        if lessons:
            self.cold.add([(l["id"], l["text"]) for l in lessons])

    def query(self, text, k=5):
        hot = self.hot.get(self.linker.link(text))[:k]
        self.hot_tokens.append(self.hot.tokens_for(hot))
        if len(hot) >= k:
            return hot
        seen = set(hot)
        return hot + [x for x in self._cold(text, k + len(hot)) if x not in seen][:k - len(hot)]

    def _cold(self, text, k):
        return self.cold.search(text, k)

    def stats(self):
        ht = sorted(self.hot_tokens) or [0]
        return {"hot_entities": len(self.hot.entries),
                "hot_store_lessons": sum(len(d) for d in self.hot.entries.values()),
                "hot_tokens_per_query_p50": ht[len(ht) // 2], "hot_tokens_per_query_max": ht[-1]}


# ---------------------------------------------------------------------------------------------- d. vector RAG
# Real vector database: Qdrant (HNSW index + payload filtering). Embeddings are computed on the HPC, not here:
#   * bulk lesson vectors come from a precomputed .npy (rows aligned with data["lessons"])
#   * embed_queries(texts) / embed_passages(texts) are hooks the HPC runner supplies (model lives there)
# QDRANT_URL selects the server (e.g. http://hpc-node:6333); unset = qdrant-client local mode (smoke tests only —
# local mode is a brute-force Python store, not HNSW, so its latency numbers are NOT representative).

class Vector:
    name = "vector"

    def __init__(self, embeddings=None, embed_queries=None, embed_passages=None, url=None, collection="lessons",
                 hnsw_m=16, ef_construct=128, ef_search=64, **_):
        from qdrant_client import QdrantClient
        self.url = url or os.getenv("QDRANT_URL")
        self.client = QdrantClient(url=self.url, prefer_grpc=True, timeout=30) if self.url else QdrantClient(":memory:")
        self.collection = collection
        self.pre = embeddings
        self.embed_queries = embed_queries
        self.embed_passages = embed_passages
        self.hnsw = (hnsw_m, ef_construct, ef_search)
        self.linker = Linker()
        self.ids: list[str] = []
        self.next_point = 0
        self.lock = threading.Lock()
        self.embed_write_ms: list[float] = []

    def build(self, data):
        from qdrant_client import models as qm
        assert self.pre is not None and len(self.pre) == len(data["lessons"]), "precomputed lesson vectors required"
        dim = self.pre.shape[1]
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        m, efc, _ = self.hnsw
        self.client.create_collection(self.collection, vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
                                      hnsw_config=qm.HnswConfigDiff(m=m, ef_construct=efc))
        self.client.create_payload_index(self.collection, "vendor", qm.PayloadSchemaType.INTEGER) if self.url else None
        self.ids = [l["id"] for l in data["lessons"]]
        payload = [{"lid": l["id"], "vendor": -1 if l["vendor"] is None else l["vendor"]} for l in data["lessons"]]
        self.client.upload_collection(self.collection, vectors=self.pre, payload=payload,
                                      ids=list(range(len(self.ids))), batch_size=512, parallel=4 if self.url else 1)
        self.next_point = len(self.ids)
        for vid, name in enumerate(data["vendor_names"]):
            self.linker.add(name, vid)

    def link_vendor(self, name, vid):
        self.linker.add(name, vid)

    def add_run(self, run_id, vendor, ents, lessons):
        if not lessons:
            return
        import time
        from qdrant_client import models as qm
        t = time.perf_counter()
        vecs = self.embed_passages([l["text"] for l in lessons])   # the real cost of the vector write path
        self.embed_write_ms.append((time.perf_counter() - t) * 1000)
        with self.lock:
            start = self.next_point
            self.next_point += len(lessons)
        self.client.upsert(self.collection, wait=True, points=[
            qm.PointStruct(id=start + i, vector=list(map(float, v)), payload={"lid": l["id"], "vendor": vendor})
            for i, (v, l) in enumerate(zip(vecs, lessons))])

    def query(self, text, k=5, vendor_filter=None):
        from qdrant_client import models as qm
        q = self.embed_queries([text])[0]
        flt = None
        if vendor_filter is not None:
            flt = qm.Filter(must=[qm.FieldCondition(key="vendor", match=qm.MatchValue(value=vendor_filter))])
        res = self.client.query_points(self.collection, query=list(map(float, q)), limit=k, query_filter=flt,
                                       search_params=qm.SearchParams(hnsw_ef=self.hnsw[2]), with_payload=True)
        return [p.payload["lid"] for p in res.points]

    def stats(self):
        w = sorted(self.embed_write_ms) or [0]
        return {"embed_on_write_ms_p50": round(w[len(w) // 2], 2), "qdrant": self.url or "local-mode(smoke only)"}


# ---------------------------------------------------------------------------------------------- e. hybrid
class Hybrid(TwoTier):
    """Two-tier HOT + vector-database COLD (Qdrant)."""
    name = "hybrid"

    def __init__(self, workdir=".", per_entity=5, hub_cap=1000, **kw):
        self.hot = Hot(per_entity, hub_cap)
        self.vec = Vector(collection="lessons_hybrid", **kw)
        self.linker = self.vec.linker
        self.hot_tokens = []

    def build(self, data):
        ents_by_run = defaultdict(list)
        for run, ent in data["facts"]:
            ents_by_run[run].append(ent)
        lessons_by_run = defaultdict(list)
        for l in data["lessons"]:
            lessons_by_run[l["run"]].append(l)
        for run, _ in data["runs"]:
            self.hot.add(ents_by_run[run], lessons_by_run.get(run, []))
        self.vec.build(data)

    def add_run(self, run_id, vendor, ents, lessons):
        self.hot.add(ents, lessons)
        self.vec.add_run(run_id, vendor, ents, lessons)

    def _cold(self, text, k):
        return self.vec.query(text, k)

    def stats(self):
        return {**super().stats(), **self.vec.stats()}


BACKENDS = {"keyword": KeywordScan, "graph": Graph, "graph_uncapped": lambda **kw: Graph(capped=False, **kw),
            "two_tier": TwoTier, "vector": Vector, "hybrid": Hybrid}
