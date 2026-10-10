"""Local embedding hooks (fastembed, BAAI/bge-small-en-v1.5, ONNX on CPU — no torch).

Bulk embedding is data-parallel across processes (fastembed `parallel=`); single-item calls on the read/write path
use one in-process model (onnxruntime runs intra-op threads). Queries get bge's retrieval instruction prefix via
fastembed's query_embed; passages don't.
"""

import os
import threading

import numpy as np

MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
_m = None
_lock = threading.Lock()


def model():
    global _m
    with _lock:
        if _m is None:
            from fastembed import TextEmbedding
            _m = TextEmbedding(MODEL, threads=int(os.getenv("EMBED_THREADS", "2")))
        return _m


def embed_passages(texts, batch_size=64):
    return np.asarray(list(model().embed(texts, batch_size=batch_size)), dtype=np.float32)


def embed_queries(texts):
    return np.asarray(list(model().query_embed(texts)), dtype=np.float32)


def embed_bulk(texts, parallel=None, batch_size=256):
    """Data-parallel bulk embedding (parallel=0 -> all cores)."""
    from fastembed import TextEmbedding
    m = TextEmbedding(MODEL)
    return np.asarray(list(m.embed(texts, batch_size=batch_size, parallel=parallel)), dtype=np.float32)
