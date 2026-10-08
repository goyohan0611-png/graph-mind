"""The on-device embedder: multilingual MiniLM, mean pooling, vectors cached on disk.

No text leaves the machine. Split out of the v0.7.3 research script (semantic_grounding_v073.py),
which still imports it from here, so the product no longer pulls that script's experiment chain.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import math
import threading

from associative_memory import _terms
from vector_cache import VectorCache, convert

LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
ACTIVATE = dict(seed_limit=12, hops=2, fanout=8, max_nodes=24, limit=8)  # frozen v0.7.1 params
SEED_LIMIT = ACTIVATE["seed_limit"]


class LocalEmbedder:
    """On-device multilingual embeddings via raw transformers (mean pooling, L2-normalized).
    No text leaves the machine — required by the user-owned local-brain promise."""

    _model = None
    _tok = None
    _lock = threading.RLock()  # model load + cache writes are not thread-safe

    FLUSH_EVERY = 5000   # vectors between writes: bounds lost work without rewriting per call

    def __init__(self, cache_path, model=LOCAL_MODEL):
        # float32 rows in a sibling directory: the old JSON cache turned 1.1GB on disk into 3-4GB
        # of Python float objects and got two evaluation runs killed for memory pressure.
        self.cache_path = Path(cache_path)
        self.model_name = model
        self.cache = VectorCache(self.cache_path.with_suffix(".vec"), model=model)
        if not len(self.cache) and self.cache_path.exists():
            self.cache = convert(self.cache_path, self.cache_path.with_suffix(".vec"), model)
        self._since = 0

    def _load(self):
        if LocalEmbedder._model is None:
            import torch
            from transformers import AutoModel, AutoTokenizer
            LocalEmbedder._tok = AutoTokenizer.from_pretrained(self.model_name)
            LocalEmbedder._model = AutoModel.from_pretrained(self.model_name).eval()
            LocalEmbedder._torch = torch

    def _key(self, text):
        return hashlib.sha256((self.model_name + "\0" + text).encode("utf-8")).hexdigest()

    def _encode(self, texts):
        import torch
        torch = LocalEmbedder._torch
        batch = LocalEmbedder._tok(texts, padding=True, truncation=True,
                                   max_length=256, return_tensors="pt")
        with torch.no_grad():
            out = LocalEmbedder._model(**batch)
        mask = batch["attention_mask"].unsqueeze(-1).float()
        emb = (out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        emb = torch.nn.functional.normalize(emb, dim=1)
        return emb.tolist()

    def embed(self, texts):
        texts = [t.strip()[:2000] for t in texts]
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.cache]
        if missing:
          with LocalEmbedder._lock:
            missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.cache]
            self._load()
            for start in range(0, len(missing), 128):  # larger batch = higher CPU throughput
                chunk = missing[start:start + 128]
                for text, vector in zip(chunk, self._encode(chunk)):
                    self.cache.put(self._key(text), vector)
                    self._since += 1
            if self._since >= LocalEmbedder.FLUSH_EVERY:
                self.flush()
        return [self.cache.get(self._key(t)) for t in texts]

    def flush(self):
        with LocalEmbedder._lock:
            self.cache.flush()
            self._since = 0


def cosine(a, b):
    dot = sum(i * j for i, j in zip(a, b))
    na = math.sqrt(sum(i * i for i in a))
    nb = math.sqrt(sum(j * j for j in b))
    return dot / (na * nb) if na and nb else 0.0


def semantic_coverage(cue_vecs, seed_vecs):
    """Mean over cues of the best cosine similarity of that cue to any seed text."""
    if not cue_vecs or not seed_vecs:
        return 0.0
    return sum(max(cosine(c, s) for s in seed_vecs) for c in cue_vecs) / len(cue_vecs)


def _seed_texts(index, query, cues):
    terms = _terms(" ".join([query, *cues]))
    if not terms:
        return []
    expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    rows = index.db.execute(
        """SELECT associative_engrams_fts.text AS text
           FROM associative_engrams_fts JOIN associative_engrams e
             ON e.engram_id=associative_engrams_fts.engram_id
           WHERE associative_engrams_fts MATCH ?
           ORDER BY bm25(associative_engrams_fts), e.revision DESC LIMIT ?""",
        [expression, SEED_LIMIT]).fetchall()
    return [r["text"] for r in rows]
