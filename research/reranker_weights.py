"""The shipped re-ranker's weights: learned on all of LoCoMo (evidence labels), checked on
LongMemEval_S. Only features the product has for every question: no personal history, and no
position in the conversation (a LoCoMo habit, not a property of memory).

    python research/reranker_weights.py
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402

KEEP = [0, 1, 2, 3, 4]          # rrf_meaning, rrf_words, similarity, both, overlap


def main() -> None:
    lc.background()
    data = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    questions = [q for q in data["questions"] if q["evidence"]]
    examples = []
    for conv in sorted({q["conv"] for q in questions}):
        lc.collect([q for q in questions if q["conv"] == conv], data["turns"], "oracle", lc.PRIOR,
                   lc.History(), examples)
    examples = [(pos[:, KEEP], neg[:, KEEP]) for pos, neg in examples]
    prior = lc.PRIOR[KEEP]
    w = lc.fit(examples, prior, strength=0.01)
    print("weights:", {lc.NAMES[i]: round(float(v), 3) for i, v in zip(KEEP, w)})
    full = np.zeros(len(lc.NAMES))
    full[KEEP] = w
    lme = json.loads(lc.POOLS.with_name("lme_pools.json").read_text(encoding="utf-8"))
    rows = [q for q in lme["questions"] if q["evidence"]]
    for name, weights, pools, group in (
            ("LongMemEval_S", None, lme["turns"], rows),
            ("LoCoMo (trained on)", None, data["turns"], questions)):
        for arm, wt in (("shipped", lc.PRIOR), ("re-ranked", full)):
            m = lc.score(group, pools, wt, lc.History())
            print(f"{name:<22}{arm:<11}{len(group):>5}{m['hit@5']:>8.1%}{m['hit@10']:>8.1%}"
                  f"{m['hit@20']:>8.1%}{m['hit@30']:>8.1%}{m['mrr']:>8.3f}")


if __name__ == "__main__":
    main()
