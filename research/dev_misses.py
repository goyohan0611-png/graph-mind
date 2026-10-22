"""The dev half's misses of a run pair, with whether the answer sessions reached the packet.
Usage: python research/dev_misses.py RUN[,RUN2]   (looks at the dev half only)"""
import json
import sys

from split_eval import RUNS, half, scores


def rows(path):
    # split on newlines only: str.splitlines also breaks at U+2028 inside a passage's text
    return {r["question_id"]: r for r in (json.loads(line) for line in
            path.read_text(encoding="utf-8").split("\n") if line.strip())}


names = sys.argv[1]
first = RUNS / names.split(",")[0]
questions = {q["question_id"]: q for q in json.loads(
    (first / "questions.json").read_text(encoding="utf-8"))}
spans, answers = rows(first / "spans.jsonl"), rows(first / "answers.jsonl")
score = scores(names)
for qid in sorted(q for q in score if half(q) == "dev" and score[q] < 1):
    q = questions[qid]
    got = {p["session_id"] for p in spans[qid]["passages"]}
    need = q.get("answer_session_ids") or []
    reply = (answers[qid].get("result") or {}).get("answer", "")
    print(json.dumps({"id": qid, "score": score[qid], "type": q["question_type"],
                      "sessions": f"{len(got & set(need))}/{len(need)}",
                      "q": q["question"], "gold": str(q["answer"])[:120],
                      "said": str(reply)[:120]}, ensure_ascii=False))
