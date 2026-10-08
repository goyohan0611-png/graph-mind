"""Productized semantic associative recall (v0.7.3 validated pipeline).

Wraps the validated arc — FTS-seeded candidates -> local-embedding semantic gate -> query
semantic re-ranking — into one callable the MCP server can route vague personal recall through.
Everything stays on-device: the embedder is local by default and the deterministic UNKNOWN
boundary is preserved (abstain below the semantic coverage threshold).

The embedder is injected (any object with .embed(list[str]) -> list[list[float]]) so tests run
without torch or network.  In production `default_embedder(db_path)` returns a cached local model.

Validated on dev(24)+blind(22): blind vague Hit@5 0%(literal) -> 50%, exact 100%, control
false-association 0%, at gate 0.35 on the multilingual MiniLM model.  Numbers are from an 8-vague
eval — treat as a strong direction, harden on a larger untouched set before making it the default.
"""
from __future__ import annotations

from pathlib import Path

from semantic_grounding_v073 import (ACTIVATE, LocalEmbedder, cosine,
    _seed_texts, semantic_coverage)

DEFAULT_THRESHOLD = 0.35   # pre-registered on the local MiniLM scale (dev safe window [0.30,0.40])
DEFAULT_POOL = 24          # = max_nodes; fixed a priori
# The 0.7.3 recall experiments reported Hit@5 and trimmed hard, which left the caller ~250 tokens of
# evidence. The LongMemEval work then measured what a reader actually needs to answer from memory:
# ~15 verbatim source spans, ~4.5k tokens, which took untouched accuracy from 65.8% to 83.3%. These
# defaults follow that evidence; pass limit/excerpt_chars to go back to the lean packet.
RESULT_LIMIT = 12          # grounded candidates returned (was 5, tuned for a Hit@5 metric)
PACKET_EXCERPT_CHARS = 800  # per-candidate verbatim characters (was 200)
PACKET_CHAR_BUDGET = 12000  # hard ceiling on the packet so a long memory cannot flood the caller
# The validated activation params (differ from brain_associate defaults on purpose).
_ACT = {**ACTIVATE, "limit": DEFAULT_POOL}


def default_embedder(db_path):
    """Local, on-device embedder with a cache persisted beside the memory DB."""
    return LocalEmbedder(Path(db_path).with_name(Path(db_path).stem + ".semantic-embed-cache.json"))


def semantic_associate(index, query, concept_cues, *, embedder,
                       scopes=None, threshold=DEFAULT_THRESHOLD, pool=DEFAULT_POOL,
                       limit=RESULT_LIMIT, excerpt_chars=PACKET_EXCERPT_CHARS):
    """Return KNOWN(reranked candidates) or a fail-closed UNKNOWN, like activate()."""
    cues = [c for c in (concept_cues or []) if isinstance(c, str) and c.strip()]
    seed_texts = _seed_texts(index, query, cues) if cues else []
    base = {"grounding": "semantic", "threshold": threshold,
            "concept_cues_used": cues, "results": []}
    if not cues:
        return {**base, "status": "UNKNOWN", "reason": "PLANNER_ABSTAINED",
                "semantic_coverage": 0.0}
    if not seed_texts:
        return {**base, "status": "UNKNOWN", "reason": "NO_ACTIVATION_SEED",
                "semantic_coverage": 0.0}
    vecs = embedder.embed(cues + seed_texts)
    sem_cov = semantic_coverage(vecs[:len(cues)], vecs[len(cues):])
    if sem_cov < threshold:
        return {**base, "status": "UNKNOWN", "reason": "INSUFFICIENT_SEMANTIC_COVERAGE",
                "semantic_coverage": round(sem_cov, 6)}
    activated = index.activate(query, scopes=scopes, concept_cues=cues,
                               minimum_concept_coverage=0.0, coverage_mode="single", **_ACT)
    cand = activated["results"]
    if not cand:
        return {**base, "status": "UNKNOWN", "reason": "NO_ACTIVATION_CANDIDATES",
                "semantic_coverage": round(sem_cov, 6)}
    qv = embedder.embed([query])[0]
    cand_vecs = embedder.embed([c["text_excerpt"] for c in cand])
    order = sorted(range(len(cand)), key=lambda i: cosine(qv, cand_vecs[i]), reverse=True)
    reranked, spent = [], 0
    for index_of in order[:pool]:
        if len(reranked) >= limit or spent >= PACKET_CHAR_BUDGET:
            break
        candidate = cand[index_of]
        if isinstance(candidate.get("text_excerpt"), str):
            candidate["text_excerpt"] = candidate["text_excerpt"][:excerpt_chars]
            spent += len(candidate["text_excerpt"])
        reranked.append(candidate)
    return {**base, "status": "KNOWN", "reason": "SEMANTIC_ACTIVATION_COMPLETED",
            "semantic_coverage": round(sem_cov, 6),
            "evidence_status": "CANDIDATES_REQUIRE_SOURCE_VERIFICATION",
            "results": reranked}


def auto_associate(index, query, concept_cues, *, embedder,
                   literal_gate=0.75, semantic_threshold=DEFAULT_THRESHOLD, scopes=None):
    """Ops-friendly cascade: try the cheap literal gate first; fall back to semantic only when
    literal abstains. The local model loads lazily, so queries literal can answer never pay for it.
    (On our evals semantic dominates both slices, so `semantic` is the accuracy default; use `auto`
    when the deployment prefers to avoid the embedder on easy queries.)"""
    cues = [c for c in (concept_cues or []) if isinstance(c, str) and c.strip()]
    if cues:
        lit = index.activate(query, scopes=scopes, concept_cues=cues,
                             minimum_concept_coverage=literal_gate, coverage_mode="single", **ACTIVATE)
        if lit.get("status") == "KNOWN" and lit.get("results"):
            lit["grounding"] = "auto:literal"
            lit["evidence_status"] = "CANDIDATES_REQUIRE_SOURCE_VERIFICATION"
            return lit
    result = semantic_associate(index, query, cues, embedder=embedder,
                                scopes=scopes, threshold=semantic_threshold)
    result["grounding"] = "auto:semantic"
    return result
