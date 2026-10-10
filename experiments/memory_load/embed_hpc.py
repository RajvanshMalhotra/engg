"""Embed an exported JSONL (one {"i","text"} per line) into a .npy aligned by line. Run on the HPC.

    python embed_hpc.py data/export_100000_lessons.jsonl data/lessons_100000.npy
"""
import json
import sys
import time

import numpy as np

from hpc_hooks import embed_passages

src, dst = sys.argv[1], sys.argv[2]
texts = [json.loads(l)["text"] for l in open(src)]
t = time.time()
vecs = embed_passages(texts)
np.save(dst, vecs)
print(f"{len(texts)} texts -> {dst} {vecs.shape} in {time.time() - t:.1f}s")
