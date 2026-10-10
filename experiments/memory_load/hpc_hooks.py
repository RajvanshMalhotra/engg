"""Embedding hooks for the vector-database arms — runs on the HPC (GPU/CPU nodes), not on the laptop.

    # on the HPC
    pip install qdrant-client sentence-transformers numpy psutil
    ./qdrant &                                         # single binary, no root/docker; listens on :6333/:6334
    python embed_hpc.py data/export_100000_lessons.jsonl data/lessons_100000.npy
    QDRANT_URL=http://localhost:6333 python loadtest.py --backends vector,hybrid --n 100000

Model is configurable with EMBED_MODEL (default BAAI/bge-small-en-v1.5, 384-d). Queries use the bge query
instruction prefix; passages don't.
"""

import os

import numpy as np

MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
QUERY_PREFIX = os.getenv("EMBED_QUERY_PREFIX", "Represent this sentence for searching relevant passages: ")
_model = None


def _m():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_passages(texts: list[str]) -> np.ndarray:
    return np.asarray(_m().encode(texts, batch_size=256, normalize_embeddings=True), dtype=np.float32)


def embed_queries(texts: list[str]) -> np.ndarray:
    return np.asarray(_m().encode([QUERY_PREFIX + t for t in texts], normalize_embeddings=True), dtype=np.float32)
