"""Judge past runs on a fixed half of the 500 questions that no fix was designed on.

The split is sha256(question_id): even first hex digit -> "dev" (look at misses here), odd ->
"held" (judge here only). Usage: python research/split_eval.py BASE[,BASE2] ARM[,ARM2] ...
Each side may name several runs (repeats); a question scores the mean over them.
"""
from math import comb
from pathlib import Path
import hashlib
import json
import sys

RUNS = Path(__file__).parent / "runs"


def half(qid: str) -> str:
    return "dev" if int(hashlib.sha256(qid.encode()).hexdigest()[0], 16) % 2 == 0 else "held"


def scores(names: str) -> dict:
    """question_id -> mean label over the named runs (only questions judged in all of them)."""
    tables = []
    for name in names.split(","):
        lines = (RUNS / name / "official-judge.jsonl").read_text(encoding="utf-8").splitlines()
        tables.append({r["question_id"]: bool(r["label"]) for r in map(json.loads, lines)
                       if "label" in r})                 # judge errors: left out
    common = set.intersection(*(set(t) for t in tables))
    return {q: sum(t[q] for t in tables) / len(tables) for q in common}


def sign_p(better: int, worse: int) -> float:
    n, k = better + worse, min(better, worse)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0


def compare(base: str, arm: str) -> dict:
    b, a = scores(base), scores(arm)
    out = {}
    for part in ("dev", "held"):
        qs = [q for q in b if q in a and half(q) == part]
        better = sum(a[q] > b[q] for q in qs)
        worse = sum(a[q] < b[q] for q in qs)
        out[part] = {"n": len(qs), "base": round(sum(b[q] for q in qs), 1),
                     "arm": round(sum(a[q] for q in qs), 1), "better": better, "worse": worse,
                     "p": round(sign_p(better, worse), 3)}
    return out


if __name__ == "__main__":
    base, *arms = sys.argv[1:]
    for arm in arms:
        print(arm, json.dumps(compare(base, arm)))
