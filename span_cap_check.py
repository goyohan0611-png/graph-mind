"""How short may an evidence span be cut? Offline, no API, no noise.

packet_shape_ablation.py answers how MANY spans to send, but it cannot answer how LONG they may be:
its hit test looks for a marker taken from characters 40-120 of the turn, which survives every cap it
tries, so it reports the same coverage for 300 and 1000 characters by construction. The length
question is a different one — does the truncation keep the sentence that answers? — and the benchmark
states the gold answer, so it is decidable exactly.

Reports, for each cap, the share of answer-bearing turns whose gold answer still falls inside it.

    python span_cap_check.py
"""
import json
import re
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from longmemeval_adapter import load_instances

IDS = Path("runs/graph-v3.2-untouched-eval-v2/preregistration.json")
CAPS = (200, 300, 400, 500, 600, 800, 1000)


def answer_tokens(answer: str) -> list[str]:
    """Words worth searching for: numbers, prices, dates and names, not 'the' or 'and'."""
    return [t for t in re.findall(r"[A-Za-z0-9$%.:/-]{3,}", answer.lower())
            if not t.isalpha() or len(t) > 3]


def positions() -> list[int]:
    """Where the answer first appears in each answer-bearing turn of the dev2 haystacks."""
    ids = set(json.loads(IDS.read_text(encoding="utf-8"))["question_ids"])
    found = []
    for row in load_instances(pipe.DATA):
        if row["question_id"] not in ids:
            continue
        wanted = answer_tokens(str(row.get("answer", "")))
        if not wanted:
            continue
        for turns in row["haystack_sessions"]:
            for turn in turns:
                if not turn.get("has_answer"):
                    continue
                text = turn["content"].lower()
                hits = [text.find(w) for w in wanted if text.find(w) >= 0]
                if hits:          # turns whose answer is a paraphrase are not decidable here
                    found.append(min(hits))
    return found


def main():
    found = positions()
    print(f"{len(found)} answer-bearing turns where the gold answer appears verbatim")
    for cap in CAPS:
        kept = sum(p < cap for p in found)
        mark = "  <= current" if cap == 500 else ""
        print(f"  cap {cap:>4}: {kept:3d}/{len(found)}  {kept / len(found):6.1%}{mark}")
    print("  answers past 300:", sorted(p for p in found if p >= 300))
    assert sum(p < 500 for p in found) == len(found), "500 no longer covers every answer"


if __name__ == "__main__":
    main()
