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


if __name__ == "__main__" and "--variants" not in sys.argv:
    main()


def variants() -> None:
    """Which feature sets hold on both benchmarks? LoCoMo 10x (leave one out) and LongMemEval."""
    lc.background()
    train = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    big = json.loads(lc.POOLS.with_name("pools-mix9-pool200.json").read_text(encoding="utf-8"))
    lme = json.loads(lc.POOLS.with_name("lme_pools.json").read_text(encoding="utf-8"))
    rows = [q for q in lme["questions"] if q["evidence"]]
    qs = [q for q in train["questions"] if q["evidence"]]
    sets = {
        "shipped 5": v2.SHIPPED,
        "+ rare, prefix": v2.SHIPPED + ["rare_overlap", "prefix_overlap"],
        "+ rare, prefix, neighbour": v2.SHIPPED + ["rare_overlap", "prefix_overlap", "neighbour"],
        "all but length": [n for n in v2.NAMES if n != "length"],
        "all but length, sessions": [n for n in v2.NAMES
                                     if n not in ("length", "session_hits", "session_best")],
    }
    print("\nfeature set                    LoCoMo 10x (5/10/30)       LongMemEval (5/10/30)")
    for label, names in sets.items():
        cols = [v2.NAMES.index(n) for n in names]
        loco = leave_one_out(train, big, cols)
        prior = np.zeros(len(cols))
        prior[:2] = 1
        w = v2.fit_linear(v2.pairs(qs, train["turns"], 50, cols), prior)
        m = v2.evaluate(rows, lme["turns"], 50, lambda x, w=w: x @ w, cols)
        print(f"  {label:<28} {loco[5]:.1%} / {loco[10]:.1%} / {loco[30]:.1%}    "
              f"{m[5]:.1%} / {m[10]:.1%} / {m[30]:.1%}   "
              + str({n: round(float(v), 2) for n, v in zip(names, w)}), flush=True)


if __name__ == "__main__" and "--variants" in sys.argv:
    variants()


def joint() -> None:
    """Learn on LoCoMo (minus the scored conversation) plus half of LongMemEval; score LoCoMo 10x
    and the other half of LongMemEval. Weights that must hold on both."""
    import random
    lc.background()
    train = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    big = json.loads(lc.POOLS.with_name("pools-mix9-pool200.json").read_text(encoding="utf-8"))
    lme = json.loads(lc.POOLS.with_name("lme_pools.json").read_text(encoding="utf-8"))
    rows = [q for q in lme["questions"] if q["evidence"]]
    random.Random(7).shuffle(rows)
    lme_train, lme_test = rows[:len(rows) // 2], rows[len(rows) // 2:]
    qs = [q for q in train["questions"] if q["evidence"]]
    sets = {"shipped 5": v2.SHIPPED,
            "+ rare, prefix": v2.SHIPPED + ["rare_overlap", "prefix_overlap"],
            "all but length": [n for n in v2.NAMES if n != "length"],
            "all": v2.NAMES}
    print("\njoint training   LoCoMo 10x held-out conv (5/10/30)   LongMemEval held-out half")
    for label, names in sets.items():
        cols = [v2.NAMES.index(n) for n in names]
        prior = np.zeros(len(cols))
        prior[:2] = 1
        lme_pairs = v2.pairs(lme_train, lme["turns"], 50, cols)
        test_q = [q for q in big["questions"] if q["evidence"]]
        total = {5: 0.0, 10: 0.0, 30: 0.0}
        for c in sorted({q["conv"] for q in test_q}):
            loco_pairs = v2.pairs([q for q in qs if q["conv"] != c], train["turns"], 50, cols)
            # each benchmark weighs the same, whatever its number of pairs
            both = np.concatenate([loco_pairs, lme_pairs[np.random.default_rng(0).choice(
                len(lme_pairs), len(loco_pairs))]])
            w = v2.fit_linear(both, prior)
            mine = [q for q in test_q if q["conv"] == c]
            m = v2.evaluate(mine, big["turns"], 50, lambda x, w=w: x @ w, cols)
            for k in total:
                total[k] += m[k] * len(mine) / len(test_q)
        m = v2.evaluate(lme_test, lme["turns"], 50, lambda x, w=w: x @ w, cols)
        f = v2.evaluate(lme_test, lme["turns"], 50, lambda x: x[:, 0] + x[:, 1], cols)
        print(f"  {label:<16} {total[5]:.1%} / {total[10]:.1%} / {total[30]:.1%}        "
              f"{m[5]:.1%} / {m[10]:.1%} / {m[30]:.1%}  (fusion {f[5]:.1%} / {f[10]:.1%} / "
              f"{f[30]:.1%})  " + str({n: round(float(v), 2) for n, v in zip(names, w)}),
              flush=True)


if __name__ == "__main__" and "--joint" in sys.argv:
    joint()


SHIP_V2 = v2.SHIPPED + ["rare_overlap", "prefix_overlap", "session_hits", "session_best"]


def ship() -> None:
    """The set the product can compute exactly (no neighbour: real ordinals are not consecutive).
    Joint weights on all of LoCoMo + all of LongMemEval, printed for mcp_server.RERANK2; and the
    held-out check as in joint()."""
    import random
    lc.background()
    cols = [v2.NAMES.index(n) for n in SHIP_V2]
    prior = np.zeros(len(cols))
    prior[:2] = 1
    e5 = "-multilingual-e5-small" if "--e5" in sys.argv else ""
    loco = json.loads(lc.POOLS.with_name(f"pools{e5}.json").read_text(encoding="utf-8"))
    big = json.loads(lc.POOLS.with_name(f"pools-mix9{e5}-pool200.json")
                     .read_text(encoding="utf-8"))
    lme = json.loads(lc.POOLS.with_name(f"lme_pools{e5}.json").read_text(encoding="utf-8"))
    qs = [q for q in loco["questions"] if q["evidence"]]
    rows = [q for q in lme["questions"] if q["evidence"]]
    random.Random(7).shuffle(rows)
    half = len(rows) // 2
    for label, data, group in (("LoCoMo 10x", big, [q for q in big["questions"] if q["evidence"]]),
                               ("LongMemEval half", lme, rows[half:])):
        f = v2.evaluate(group, data["turns"], 50, lambda x: x[:, 0] + x[:, 1], cols)
        print(f"fusion {label}: {f[5]:.1%} / {f[10]:.1%} / {f[30]:.1%}", flush=True)
    lme_pairs = v2.pairs(rows[:half], lme["turns"], 50, cols)
    total = {5: 0.0, 10: 0.0, 30: 0.0}
    test_q = [q for q in big["questions"] if q["evidence"]]
    for c in sorted({q["conv"] for q in test_q}):
        a = v2.pairs([q for q in qs if q["conv"] != c], loco["turns"], 50, cols)
        w = v2.fit_linear(np.concatenate(
            [a, lme_pairs[np.random.default_rng(0).choice(len(lme_pairs), len(a))]]), prior)
        mine = [q for q in test_q if q["conv"] == c]
        m = v2.evaluate(mine, big["turns"], 50, lambda x, w=w: x @ w, cols)
        for k in total:
            total[k] += m[k] * len(mine) / len(test_q)
    m = v2.evaluate(rows[half:], lme["turns"], 50, lambda x, w=w: x @ w, cols)
    print(f"held out: LoCoMo 10x {total[5]:.1%} / {total[10]:.1%} / {total[30]:.1%}   "
          f"LongMemEval half {m[5]:.1%} / {m[10]:.1%} / {m[30]:.1%}")
    a = v2.pairs(qs, loco["turns"], 50, cols)
    b = v2.pairs(rows, lme["turns"], 50, cols)
    w = v2.fit_linear(np.concatenate([a, b[np.random.default_rng(0).choice(len(b), len(a))]]),
                      prior)
    print("RERANK2 =", {n: round(float(x), 3) for n, x in zip(SHIP_V2, w)})


if __name__ == "__main__" and "--ship" in sys.argv:
    ship()
