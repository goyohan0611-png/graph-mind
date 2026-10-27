"""Does a bigger candidate pool hold recall up as the memory grows? The model still reads the first
30 turns (same tokens); only the pool the re-ranker orders grows.

Pools of 200 per search (locomo_pools.py --pool 200, memory 1x/4x/10x) are cut to 50, 100, 150
and 200. For each: ceiling (answer anywhere in the pool), plain fusion and the re-ranker (5
shipped features, each conversation scored with weights learned on the other nine at pool 50, as
shipped), answer in the first 30.

    python research/pool_probe.py
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402

KEEP = [0, 1, 2, 3, 4]


def held_out_weights() -> dict[str, np.ndarray]:
    base = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    questions = [q for q in base["questions"] if q["evidence"]]
    convs = sorted({q["conv"] for q in questions})
    weights = {}
    for conv in convs:
        examples = []
        for other in convs:
            if other != conv:
                lc.collect([q for q in questions if q["conv"] == other], base["turns"], "oracle",
                           lc.PRIOR, lc.History(), examples)
        w = lc.fit([(p[:, KEEP], n[:, KEEP]) for p, n in examples], lc.PRIOR[KEEP], strength=0.01)
        weights[conv] = np.zeros(len(lc.NAMES))
        weights[conv][KEEP] = w
    return weights


def cut(question: dict, size: int) -> dict:
    return {**question, "meaning": question["meaning"][:size], "words": question["words"][:size]}


def main() -> None:
    lc.background()
    weights = held_out_weights()
    print(f"{'memory':<8}{'pool':>6}{'ceiling':>10}{'fusion@30':>11}{'re-ranked@30':>14}"
          f"{'re-ranked@10':>14}")
    results = {}
    for mix, label in ((0, "x1"), (3, "x4"), (9, "x10")):
        name = "pools-pool200.json" if not mix else f"pools-mix{mix}-pool200.json"
        path = lc.POOLS.with_name(name)
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = [q for q in data["questions"] if q["evidence"]]
        for size in (30, 50, 75, 100, 125, 150, 175, 200):
            group = [cut(q, size) for q in questions]
            inside = np.mean([bool(set(q["evidence"]) & ({t for t, _ in q["meaning"]}
                                                          | set(q["words"]))) for q in group])
            fusion = lc.score(group, data["turns"], lc.PRIOR, lc.History())
            ranked = {"hit@30": 0.0, "hit@10": 0.0}
            for conv in sorted({q["conv"] for q in group}):
                mine = [q for q in group if q["conv"] == conv]
                m = lc.score(mine, data["turns"], weights[conv], lc.History())
                for k in ranked:
                    ranked[k] += m[k] * len(mine) / len(group)
            results[f"{label}/{size}"] = {"ceiling": inside, "fusion@30": fusion["hit@30"],
                                          **{f"re-ranked@{k[4:]}": v for k, v in ranked.items()}}
            print(f"{label:<8}{size:>6}{inside:>10.1%}{fusion['hit@30']:>11.1%}"
                  f"{ranked['hit@30']:>14.1%}{ranked['hit@10']:>14.1%}", flush=True)
    lc.POOLS.with_name("pool.json").write_text(json.dumps(results, indent=1) + "\n",
                                                encoding="utf-8")


if __name__ == "__main__":
    main()
