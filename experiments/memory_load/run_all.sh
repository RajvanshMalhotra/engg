#!/usr/bin/env bash
# Reproduce docs/MEMORY_LOAD_TEST.md (laptop run). ~25 min on an M2 / 8 GB, most of it CPU embedding.
set -euo pipefail
cd "$(dirname "$0")"

# 1. synthetic companies (sharded across processes) + text exports for embedding
uv run python generate.py 100000
uv run python generate.py 10000

# 2. lesson embeddings: fastembed BAAI/bge-small-en-v1.5 (ONNX, CPU), data-parallel; writes data/lessons_<n>.npy
uv run --group experiments python embed_local.py 100000 10000

# 3. local arms (keyword, graph capped/uncapped, two-tier) + FTS multi-process readers
uv run --group experiments python loadtest.py --n 100000 --backends keyword,graph,two_tier

# 4. vector DB arms against a real Qdrant server (binary in bin/, see README section in the doc)
mkdir -p qdrant_storage
QDRANT__STORAGE__STORAGE_PATH="$PWD/qdrant_storage" QDRANT__TELEMETRY_DISABLED=true ./bin/qdrant > results/qdrant.log 2>&1 &
QPID=$!
trap 'kill $QPID' EXIT
until curl -s http://127.0.0.1:6333/readyz > /dev/null; do sleep 1; done
QDRANT_URL=http://127.0.0.1:6333 EMBED_THREADS=4 uv run --group experiments python loadtest.py --n 100000 \
  --backends vector,hybrid --sweep-backends vector,hybrid --no-fts-mp
