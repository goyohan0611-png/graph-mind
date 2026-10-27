"""Does recall get better the more one person uses it? Offline, on the LoCoMo pools.

locomo_pools.py keeps, for every question, the shipped word and meaning rankings (top 50 each).
Here a small re-ranker re-orders that pool. It starts at the shipped order (reciprocal-rank
fusion) and learns, per conversation, from the questions asked before.

Each conversation's questions are shuffled (3 seeds); the last 40% are the test, never learned
from. After the first N of the rest have been asked, we measure on the test:
    hit@k   share of test questions with an answering turn in the first k (5, 10, 30)
    mrr     mean 1/rank of the first answering turn (0 if not in the pool)

The learning signal is only what the product can see after an answer: which shown turns (the
first 30) the answer used. Two versions:
    oracle  the turns the benchmark labels as evidence, if shown (an upper bound)
    proxy   shown turns that contain most of the reference answer's words (what a text match
            against the model's reply would give; the benchmark's short answer stands in)
Arms: shipped, personal (learns from this conversation only), general (learned on the other nine
conversations, no personal history), general+personal.

    python research/learning_curve.py
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import os
import random
import re
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")     # one core at most
import numpy as np  # noqa: E402

POOLS = Path(__file__).resolve().parent / "runs/graph-v9.20-learning/pools.json"
SHOWN = 30
STEPS = [0, 10, 25, 50, 100, None]          # None: every non-test question
SEEDS = [1, 2, 3]
STOP = set("a an the of to in on at for and or but is are was were be been do does did what when "
           "where who whom which why how that this with from by as it its his her their they them "
           "he she you your i my me we our has have had not no yes would could should will can "
           "about after before during into than then there so".split())
NAMES = ["rrf_meaning", "rrf_words", "similarity", "both", "overlap", "position",
         "used_before", "session_used", "near_used", "term_link"]
PRIOR = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0], dtype=float)   # = the shipped fusion


def terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W_]{2,}", text.casefold()) if w not in STOP}


class History:
    """What one user's earlier questions taught: which turns their answers used."""

    def __init__(self):
        self.used, self.sessions, self.links = {}, {}, {}

    def learn(self, question: dict, positives: list[str]) -> None:
        for tid in positives:
            self.used[tid] = self.used.get(tid, 0) + 1
            session = tid.split(":")[0]
            self.sessions[session] = self.sessions.get(session, 0) + 1
            for term in terms(question["question"]):
                self.links.setdefault(term, {})
                self.links[term][tid] = self.links[term].get(tid, 0) + 1


def turn_number(tid: str) -> tuple[str, int]:
    session, _, number = tid.partition(":")
    return session, int(number) if number.isdigit() else 0


def features(question: dict, texts: dict, history: History) -> tuple[list[str], np.ndarray]:
    meaning = {tid: (rank, sim) for rank, (tid, sim) in enumerate(question["meaning"])}
    words = {tid: rank for rank, tid in enumerate(question["words"])}
    pool = list(dict.fromkeys([t for t, _ in question["meaning"]] + question["words"]))
    asked = terms(question["question"])
    last = max(len(texts) - 1, 1)
    where = {tid: v.get("pos", i / last) for i, (tid, v) in enumerate(texts.items())}
    near = {}
    for tid in history.used:
        session, number = turn_number(tid)
        for step in (-2, -1, 1, 2):
            near[f"{session}:{number + step}"] = 1
    rows = []
    for tid in pool:
        rank_m, sim = meaning.get(tid, (None, 0.0))
        rank_w = words.get(tid)
        text = texts.get(tid, {}).get("text", "")
        link = sum(history.links.get(term, {}).get(tid, 0) for term in asked)
        rows.append([60 / (60 + rank_m) if rank_m is not None else 0.0,
                     60 / (60 + rank_w) if rank_w is not None else 0.0,
                     sim,
                     float(rank_m is not None and rank_w is not None),
                     len(asked & terms(text)) / max(len(asked), 1),
                     where.get(tid, 0.0),
                     math.log1p(history.used.get(tid, 0)),
                     math.log1p(history.sessions.get(tid.split(":")[0], 0)),
                     float(tid in near),
                     math.log1p(link)])
    return pool, np.array(rows, dtype=float).reshape(len(pool), len(NAMES))


def rank(pool: list[str], x: np.ndarray, w: np.ndarray) -> list[str]:
    score = x @ w
    return [pool[i] for i in sorted(range(len(pool)), key=lambda i: (-score[i], i))]


REPLIES: dict[tuple[str, int], str] = {}
_RARITY: dict[str, dict[str, float]] = {}


def rarity(conv: str, mine: dict) -> dict[str, float]:
    """Inverse document frequency of each word over one user's turns."""
    if conv not in _RARITY:
        counts: dict[str, int] = {}
        for value in mine.values():
            for word in terms(value["text"]):
                counts[word] = counts.get(word, 0) + 1
        _RARITY[conv] = {w: math.log(len(mine) / (1 + c)) for w, c in counts.items()}
    return _RARITY[conv]


def positives(question: dict, shown: list[str], texts: dict, signal: str) -> list[str]:
    if signal == "oracle":
        return [t for t in shown if t in set(question["evidence"])]
    if signal == "reply":
        # the one shown turn the model's reply drew on most: the words it shares with it beyond
        # the question's own, rare words counting more (on 127 replies: right 55% of the time,
        # against 31% for "best three by shared words")
        said = terms(REPLIES.get((question["conv"], question["n"]), "")) - terms(
            question["question"])
        mine = texts[question["conv"]]
        rare = rarity(question["conv"], mine)
        weight = {t: sum(rare.get(w, 0.0) for w in said & terms(mine.get(t, {}).get("text", "")))
                  for t in shown}
        best = max(shown, key=lambda t: weight[t], default=None)
        return [best] if best is not None and weight[best] >= 4 else []
    answer = terms(question["answer"])
    if not answer:
        return []
    return [t for t in shown
            if len(answer & terms(texts[question["conv"]].get(t, {}).get("text", "")))
            >= max(1, math.ceil(0.6 * len(answer)))]


def fit(examples: list[tuple[np.ndarray, np.ndarray]], start: np.ndarray, strength: float,
        steps: int = 300, rate: float = 0.5) -> np.ndarray:
    """Pairwise logistic loss (each used turn above each unused one), pulled toward `start`."""
    pairs = [pos[:, None, :] - neg[None, :, :] for pos, neg in examples if len(pos) and len(neg)]
    if not pairs:
        return start.copy()
    diff = np.concatenate([p.reshape(-1, start.size) for p in pairs])
    w = start.copy()
    for _ in range(steps):
        margin = diff @ w
        grad = -(diff * (1 / (1 + np.exp(np.clip(margin, -30, 30))))[:, None]).mean(0)
        grad += strength * (w - start)
        w -= rate * grad
    return w


def collect(stream: list[dict], texts: dict, signal: str, w: np.ndarray, history: History,
            examples: list) -> None:
    """Ask the stream in order: show the top 30 under the current weights, learn what was used."""
    for question in stream:
        pool, x = features(question, texts[question["conv"]], history)
        shown = rank(pool, x, w)[:SHOWN]
        used = positives(question, shown, texts, signal)
        index = {t: i for i, t in enumerate(pool)}
        pos = x[[index[t] for t in used]]
        neg = x[[index[t] for t in shown if t not in used]]
        examples.append((pos, neg))
        history.learn(question, used)


def score(test: list[dict], texts: dict, w: np.ndarray, history: History) -> dict:
    out = {f"hit@{k}": 0 for k in (5, 10, 30)} | {"mrr": 0.0}
    for question in test:
        pool, x = features(question, texts[question["conv"]], history)
        order = rank(pool, x, w)
        gold = set(question["evidence"])
        first = next((i for i, t in enumerate(order) if t in gold), None)
        for k in (5, 10, 30):
            out[f"hit@{k}"] += first is not None and first < k
        out["mrr"] += 1 / (first + 1) if first is not None else 0
    return {k: v / len(test) for k, v in out.items()}


def general_weights(questions: list[dict], texts: dict, signal: str, leave: str) -> np.ndarray:
    examples = []
    for conv in sorted({q["conv"] for q in questions} - {leave}):
        collect([q for q in questions if q["conv"] == conv], texts, signal, PRIOR, History(),
                examples)
    return fit(examples, PRIOR, strength=0.01)


def background() -> None:
    """Lowest CPU priority: this must never make the PC feel slower."""
    import psutil
    psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS if sys.platform == "win32" else 19)


def transfer() -> None:
    """Weights learned on all of LoCoMo (its evidence labels), scored on LongMemEval_S pools."""
    data = json.loads(POOLS.read_text(encoding="utf-8"))
    questions = [q for q in data["questions"] if q["evidence"]]
    w = general_weights(questions, data["turns"], "oracle", leave=None)
    print("weights:", {k: round(float(v), 2) for k, v in zip(NAMES, w)})
    lme = json.loads(POOLS.with_name("lme_pools.json").read_text(encoding="utf-8"))
    rows = [q for q in lme["questions"] if q["evidence"]]
    print(f"{'type':<28}{'n':>5}  {'arm':<9}{'hit@5':>8}{'hit@10':>8}{'hit@30':>8}{'mrr':>8}")
    for kind in ["all"] + sorted({q["category"] for q in rows}):
        group = [q for q in rows if kind == "all" or q["category"] == kind]
        for arm, weights in (("shipped", PRIOR), ("general", w)):
            m = score(group, lme["turns"], weights, History())
            print(f"{kind:<28}{len(group):>5}  {arm:<9}{m['hit@5']:>8.1%}{m['hit@10']:>8.1%}"
                  f"{m['hit@30']:>8.1%}{m['mrr']:>8.3f}")


def main() -> None:
    background()
    if "--transfer" in sys.argv:
        return transfer()
    data = json.loads(POOLS.read_text(encoding="utf-8"))
    texts = data["turns"]
    questions = [q for q in data["questions"] if q["evidence"]]
    convs = sorted({q["conv"] for q in questions})
    results = {}
    signals = ["oracle", "proxy"]
    tested = convs
    if "--reply" in sys.argv:       # only the conversations Codex answered (codex_replies.py)
        for row in map(json.loads, POOLS.with_name("replies.jsonl").open(encoding="utf-8")):
            REPLIES[(row["conv"], row["n"])] = row["reply"]
        tested = sorted({conv for conv, _ in REPLIES})
        signals.append("reply")
    # the general re-ranker is trained once, offline, on labelled conversations: the evidence
    general = {c: general_weights(questions, texts, "oracle", c) for c in tested}
    for signal in signals:
        table = {}
        for seed in SEEDS:
            for conv in tested:
                mine = [q for q in questions if q["conv"] == conv]
                random.Random(seed).shuffle(mine)
                cut = int(len(mine) * 0.6)
                stream, test = mine[:cut], mine[cut:]
                for step in STEPS:
                    asked = stream[:step] if step is not None else stream
                    for arm, start in (("shipped", None), ("personal", PRIOR),
                                       ("general", general[conv]),
                                       ("general+personal", general[conv])):
                        history, examples = History(), []
                        if arm in ("personal", "general+personal"):
                            collect(asked, texts, signal, start, history, examples)
                            w = fit(examples, start, strength=1.0 / (1 + len(asked) / 25))
                        else:
                            w = PRIOR if start is None else start
                        key = (arm, "all" if step is None else step)
                        row = score(test, texts, w, history)
                        for name, value in row.items():
                            table.setdefault(key, {}).setdefault(name, []).append(value)
        results[signal] = {f"{arm}@{step}": {k: round(float(np.mean(v)), 4)
                                             for k, v in metrics.items()}
                           for (arm, step), metrics in table.items()}
        print(f"\n== signal: {signal}  ({len(tested)} conversations, "
              f"{len(SEEDS)} seeds; test = last 40% of each conversation)")
        print(f"{'arm':<18}{'asked':>6}{'hit@5':>8}{'hit@10':>8}{'hit@30':>8}{'mrr':>8}")
        for (arm, step), metrics in table.items():
            means = {k: np.mean(v) for k, v in metrics.items()}
            print(f"{arm:<18}{step!s:>6}{means['hit@5']:>8.1%}{means['hit@10']:>8.1%}"
                  f"{means['hit@30']:>8.1%}{means['mrr']:>8.3f}")
    out = POOLS.with_name("curve-reply.json" if "--reply" in sys.argv else "curve.json")
    out.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    print(f"\nwritten: {out}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
