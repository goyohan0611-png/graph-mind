"""Does learning keep recall level as the memory grows? LoCoMo pools at three memory sizes:
one conversation (pools.json), and with 3 and 9 other conversations stored beside it
(locomo_pools.py --mix 3 / --mix 9). Same questions, same evidence, more to search through.

Arms per size: shipped fusion, the shipped re-ranker (general, no history), and general+personal
after every non-test question (signal: oracle and proxy; see learning_curve.py).

    python research/scale_curve.py
"""
from __future__ import annotations

from pathlib import Path
import json
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402


def main() -> None:
    lc.background()
    print(f"{'memory':<10}{'turns':>7}  {'arm':<26}"
          f"{'hit@5':>8}{'hit@10':>8}{'hit@30':>8}{'mrr':>8}")
    results = {}
    for mix in (0, 3, 9):
        path = lc.POOLS if not mix else lc.POOLS.with_name(f"pools-mix{mix}.json")
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        texts = data["turns"]
        questions = [q for q in data["questions"] if q["evidence"]]
        convs = sorted({q["conv"] for q in questions})
        general = {c: lc.general_weights(questions, texts, "oracle", c) for c in convs}
        table = {}
        for seed in lc.SEEDS:
            for conv in convs:
                mine = [q for q in questions if q["conv"] == conv]
                random.Random(seed).shuffle(mine)
                cut = int(len(mine) * 0.6)
                stream, test = mine[:cut], mine[cut:]
                arms = [("shipped", lc.PRIOR, None), ("re-ranked (general)", general[conv], None),
                        ("general+personal oracle", general[conv], "oracle"),
                        ("general+personal proxy", general[conv], "proxy")]
                for arm, start, signal in arms:
                    history, examples = lc.History(), []
                    w = start
                    if signal:
                        lc.collect(stream, texts, signal, start, history, examples)
                        w = lc.fit(examples, start, strength=1.0 / (1 + len(stream) / 25))
                    for name, value in lc.score(test, texts, w, history).items():
                        table.setdefault(arm, {}).setdefault(name, []).append(value)
        size = round(np.mean([len([t for t in texts[c] if "/" not in t]) for c in convs])
                     * (mix + 1))
        results[mix] = {arm: {k: round(float(np.mean(v)), 4) for k, v in m.items()}
                        for arm, m in table.items()}
        for arm, m in results[mix].items():
            print(f"x{mix + 1:<9}{size:>7}  {arm:<26}{m['hit@5']:>8.1%}{m['hit@10']:>8.1%}"
                  f"{m['hit@30']:>8.1%}{m['mrr']:>8.3f}")
    out = lc.POOLS.with_name("scale.json")
    out.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
