"""Precompute lesson vectors locally (parallel across cores) and measure embedding throughput.

    uv run python embed_local.py 100000 10000
"""
import json
import sys
import time

import numpy as np

from embedder import embed_bulk

DATA = __import__("pathlib").Path(__file__).parent / "data"

if __name__ == "__main__":
    report = {}
    sample = [json.loads(l)["text"] for l in open(DATA / "export_100000_lessons.jsonl")][:4000]
    for par in (None, 2, 4, 8):
        t = time.time()
        embed_bulk(sample, parallel=par)
        dt = time.time() - t
        report[f"parallel={par}"] = round(len(sample) / dt, 1)
        print(f"throughput parallel={par}: {report[f'parallel={par}']} texts/s", flush=True)
    best = max((p for p in (None, 2, 4, 8)), key=lambda p: report[f"parallel={p}"])
    for n in map(int, sys.argv[1:]):
        texts = [json.loads(l)["text"] for l in open(DATA / f"export_{n}_lessons.jsonl")]
        t = time.time()
        vecs = embed_bulk(texts, parallel=best)
        dt = time.time() - t
        np.save(DATA / f"lessons_{n}.npy", vecs)
        report[f"bulk_{n}"] = {"texts": len(texts), "seconds": round(dt, 1), "texts_per_s": round(len(texts) / dt, 1),
                               "parallel": best, "shape": list(vecs.shape)}
        print(json.dumps(report[f"bulk_{n}"]), flush=True)
    (DATA.parent / "results" / "embedding_throughput.json").write_text(json.dumps(report, indent=1))
