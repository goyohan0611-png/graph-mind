"""Three cheap ideas for keeping recall up as one person's memory grows, on the LoCoMo pools at
1x and 10x memory (pools.json, pools-mix9.json). Each is a fixed boost on top of the general
re-ranker, after every non-test question has been asked (see learning_curve.py):

    used     turns that earlier answers used, by how often (log count)
    recent   the same, but each use fades with the questions asked since (ACT-R style, d=0.5)
    session  turns in a session earlier answers used
And the ceiling no re-ordering can pass: the share of test questions whose answer is anywhere in
the 100-turn pool.

    python research/boost_probe.py
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402

USED, SESSION = lc.NAMES.index("used_before"), lc.NAMES.index("session_used")


class Fading(lc.History):
    """used[turn] = log of summed (questions since each use + 1) ** -0.5, kept as a count-like
    number so features() can take log1p of it unchanged."""

    def __init__(self):
        super().__init__()
        self.uses, self.now = {}, 0

    def learn(self, question, positives):
        super().learn(question, positives)
        self.now += 1
        for tid in positives:
            self.uses.setdefault(tid, []).append(self.now)
        self.used = {tid: math.expm1(sum((self.now - t + 1) ** -0.5 for t in times))
                     for tid, times in self.uses.items()}


_GENERAL: dict[int, dict] = {}


def run(data, signal, boosts, history_class=lc.History):
    """`boosts`: a list of {column: amount}, each one arm. The stream is asked once per
    conversation and seed; every arm is scored on the same history."""
    texts = data["turns"]
    questions = [q for q in data["questions"] if q["evidence"]]
    convs = sorted({q["conv"] for q in questions})
    if id(data) not in _GENERAL:
        _GENERAL[id(data)] = {c: lc.general_weights(questions, texts, "oracle", c) for c in convs}
    general = _GENERAL[id(data)]
    acc = [[] for _ in boosts]
    for seed in lc.SEEDS:
        for conv in convs:
            mine = [q for q in questions if q["conv"] == conv]
            random.Random(seed).shuffle(mine)
            cut = int(len(mine) * 0.6)
            history = history_class()
            lc.collect(mine[:cut], texts, signal, general[conv], history, [])
            for i, boost in enumerate(boosts):
                w = general[conv].copy()
                for column, amount in boost.items():
                    w[column] += amount
                m = lc.score(mine[cut:], texts, w, history)
                acc[i].append([m["hit@5"], m["hit@10"], m["hit@30"]])
    return [np.mean(a, 0) for a in acc]


def ceiling(data):
    questions = [q for q in data["questions"] if q["evidence"]]
    inside = [bool(set(q["evidence"]) & ({t for t, _ in q["meaning"]} | set(q["words"])))
              for q in questions]
    return sum(inside) / len(inside)


def main() -> None:
    lc.background()
    for name in ("pools.json", "pools-mix9.json"):
        data = json.loads(lc.POOLS.with_name(name).read_text(encoding="utf-8"))
        print(f"\n{name}: answer anywhere in the pool (ceiling): {ceiling(data):.1%}")
        for signal in ("oracle", "proxy"):
            arms = [("none", lc.History, [{}])]
            arms += [(label, cls, [{column: b} for b in (0.5, 1, 2, 4)])
                     for label, column, cls in (("used", USED, lc.History),
                                                ("recent", USED, Fading),
                                                ("session", SESSION, lc.History))]
            pairs = [(1, 1), (2, 1), (1, 2), (2, 2), (4, 2)]
            arms += [(label + "+session", cls, [{USED: a, SESSION: b} for a, b in pairs])
                     for label, cls in (("used", lc.History), ("recent", Fading))]
            for label, cls, boosts in arms:
                for boost, a in zip(boosts, run(data, signal, boosts, cls)):
                    amounts = "/".join(f"{v:g}" for v in boost.values()) or "0"
                    print(f"  {signal:6} {label:15} +{amounts:<6} hit@5 {a[0]:.1%}"
                          f"  hit@10 {a[1]:.1%}  hit@30 {a[2]:.1%}", flush=True)


if __name__ == "__main__":
    main()
