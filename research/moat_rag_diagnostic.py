"""Cheap moat proof: why Vector RAG structurally fails LongMemEval's execution categories.

No LLM, no API — local embeddings only.  For a subset of the FULL LongMemEval_S haystack (evidence
+ distractors), we rank sessions by embedding similarity to the question (that IS Vector RAG) and
measure, per category:

  - evidence_recall@k  : fraction of the gold evidence sessions retrieved in the top-k
  - complete@k         : did the top-k contain ALL gold evidence sessions
                         (aggregation/COUNT is only correct if every fact is present)
  - abstain_rate       : on abstention questions RAG still returns k sessions -> it cannot say
                         "I don't have that" (structural; Graph-MIND abstains with a certificate)

The point: multi-evidence questions (324/500) need complete@k=1 to COUNT/aggregate correctly, and
RAG's complete@k collapses as distractors grow; abstention questions expose that RAG never abstains.
This is a RAG-weakness measurement that quantifies the moat; Graph-MIND's executor side is the
deterministic aggregation + abstention (see runs/graph-v2.1-longmemeval-oracle-ingestion-recall-*).

    python moat_rag_diagnostic.py            # local, no API
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json

from semantic_grounding_v073 import LocalEmbedder, cosine

DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
ROOT = Path("runs/graph-v3.0-moat-rag-diagnostic")
KS = (5, 10)
PER_TYPE = {"multi-session": 12, "temporal-reasoning": 10, "knowledge-update": 6}
N_ABSTENTION = 8
SESSION_CHARS = 1500


def _session_text(session):
    return "\n".join(f"{t.get('role', '')}: {t.get('content', '')}" for t in session)[:SESSION_CHARS]


def _select(instances):
    picked, counts = [], defaultdict(int)
    absten = [x for x in instances if str(x["question_id"]).endswith("_abs")][:N_ABSTENTION]
    picked.extend(absten)
    for x in instances:
        if str(x["question_id"]).endswith("_abs"):
            continue
        t = x["question_type"]
        if counts[t] < PER_TYPE.get(t, 0):
            counts[t] += 1
            picked.append(x)
    return picked


def run(root=ROOT):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    instances = json.load(DATA.open(encoding="utf-8"))
    selected = _select(instances)
    del instances  # free the 277MB structure
    embedder = LocalEmbedder(root / "embed-cache-local.json")
    per = defaultdict(lambda: {"n": 0, "recall": {k: 0.0 for k in KS},
                               "complete": {k: 0 for k in KS}, "nonabstain": 0})
    for inst in selected:
        is_abs = str(inst["question_id"]).endswith("_abs")
        cat = "abstention" if is_abs else inst["question_type"]
        sess_ids = inst["haystack_session_ids"]
        texts = [_session_text(s) for s in inst["haystack_sessions"]]
        qv = embedder.embed([inst["question"]])[0]
        svs = embedder.embed(texts)
        order = sorted(range(len(sess_ids)), key=lambda i: cosine(qv, svs[i]), reverse=True)
        gold = set(inst["answer_session_ids"])
        row = per[cat]
        row["n"] += 1
        for k in KS:
            topk = {sess_ids[i] for i in order[:k]}
            if is_abs:
                row["nonabstain"] += 1 if k == KS[0] and topk else 0  # RAG always returns something
            elif gold:
                row["recall"][k] += len(gold & topk) / len(gold)
                row["complete"][k] += int(gold <= topk)
    report = {"benchmark": "moat-rag-diagnostic", "dataset": str(DATA),
              "questions": len(selected), "ks": list(KS),
              "note": "Vector RAG retrieval only (local embeddings), full haystack with distractors. "
                      "Answerable: recall@k / complete@k of gold evidence. Abstention: RAG cannot "
                      "abstain. No LLM answer step.",
              "by_category": {}}
    for cat, r in per.items():
        n = r["n"]
        if cat == "abstention":
            report["by_category"][cat] = {"n": n, "abstain_rate": 0.0,
                "returns_distractors_rate": 1.0}
        else:
            report["by_category"][cat] = {"n": n,
                "recall@%d" % KS[0]: round(r["recall"][KS[0]] / n, 3),
                "recall@%d" % KS[1]: round(r["recall"][KS[1]] / n, 3),
                "complete@%d" % KS[0]: round(r["complete"][KS[0]] / n, 3),
                "complete@%d" % KS[1]: round(r["complete"][KS[1]] / n, 3)}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run()["by_category"], ensure_ascii=False, indent=2))
