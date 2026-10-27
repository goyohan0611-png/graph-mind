"""Checks for re-ranker v2 (rerank_v2.py) before it can ship:

1. Ablation: leave one new feature out at a time (LoCoMo, memory 10x, pool 50, each conversation
   scored with weights learned on the other nine).
2. Transfer: weights learned on all of LoCoMo (memory 1x), scored on LongMemEval_S pools, which
   were never trained on, against the shipped re-ranker.

    python research/rerank_v2_check.py
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402
import rerank_v2 as v2  # noqa: E402

NEW = v2.NAMES[5:]


def leave_one_out(train, test, columns, size=50):
    qs_train = [q for q in train["questions"] if q["evidence"]]
    qs_test = [q for q in test["questions"] if q["evidence"]]
    convs = sorted({q["conv"] for q in qs_test})
    prior = np.zeros(len(columns))
    prior[:2] = 1
    total = {5: 0.0, 10: 0.0, 30: 0.0}
    for c in convs:
        w = v2.fit_linear(v2.pairs([q for q in qs_train if q["conv"] != c], train["turns"], 50,
                                   columns), prior)
        mine = [q for q in qs_test if q["conv"] == c]
        m = v2.evaluate(mine, test["turns"], size, lambda x, w=w: x @ w, columns)
        for k in total:
            total[k] += m[k] * len(mine) / len(qs_test)
    return total


def main() -> None:
    lc.background()
    train = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    big = json.loads(lc.POOLS.with_name("pools-mix9-pool200.json").read_text(encoding="utf-8"))
    every = list(range(len(v2.NAMES)))
    print("ablation, memory 10x, pool 50 (hit@5 / hit@10 / hit@30)")
    for drop in [None] + NEW:
        cols = [i for i in every if v2.NAMES[i] != drop]
        m = leave_one_out(train, big, cols)
        print(f"  without {drop or '(nothing)':<16} {m[5]:.1%} / {m[10]:.1%} / {m[30]:.1%}",
              flush=True)
    qs = [q for q in train["questions"] if q["evidence"]]
    prior = np.zeros(len(every))
    prior[:2] = 1
    w_all = v2.fit_linear(v2.pairs(qs, train["turns"], 50, every), prior)
    print("\nweights learned on all of LoCoMo:",
          {n: round(float(v), 3) for n, v in zip(v2.NAMES, w_all)})
    five = [v2.NAMES.index(n) for n in v2.SHIPPED]
    w_five = v2.fit_linear(v2.pairs(qs, train["turns"], 50, five), prior[five])
    lme = json.loads(lc.POOLS.with_name("lme_pools.json").read_text(encoding="utf-8"))
    rows = [q for q in lme["questions"] if q["evidence"]]
    print("\nLongMemEval_S (never trained on), pool 50")
    for kind in ["all"] + sorted({q["category"] for q in rows}):
        group = [q for q in rows if kind == "all" or q["category"] == kind]
        for arm, scorer, cols in (("fusion", lambda x: x[:, 0] + x[:, 1], every),
                                  ("shipped", lambda x: x @ w_five, five),
                                  ("v2", lambda x: x @ w_all, every)):
            m = v2.evaluate(group, lme["turns"], 50, scorer, cols)
            print(f"  {kind:<26}{len(group):>4} {arm:<8} {m[5]:.1%} / {m[10]:.1%} / {m[30]:.1%}",
                  flush=True)


if __name__ == "__main__":
    main()
