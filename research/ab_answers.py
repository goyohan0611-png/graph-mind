"""Answer-level A/B of two recall versions on LongMemEval_S (packets from ab_packets.py).

Only questions whose two packets differ are answered: an identical packet gets the same answer
from the same reader, so it cannot change the difference. Codex (gpt-6.1-sol, low effort) reads
each packet with the benchmark's answer prompt, and the official judge prompt grades it, also
with Codex; one call at a time. The reader is not gpt-5-mini, so the absolute numbers are not
the README's 88.8%; the difference between A and B is the result.

    python research/ab_answers.py            # resumable
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from codex_replies import EMPTY, codex  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402
import longmemeval_retrieval_executor as pipe  # noqa: E402
import official_judge_v073 as judge  # noqa: E402

RUN = ROOT / "research/runs/graph-v9.22-ab"
DATA = ROOT / "research/external/longmemeval/longmemeval_s_cleaned.json"


def answer_prompt(question, passages):
    body = pipe.answer_body(question, {}, [], None, None, passages)
    system, user = body["input"][0]["content"], body["input"][1]["content"]
    schema = json.dumps(body["text"]["format"]["schema"])
    return (system + "\n\nDo not run any commands or read files; answer from the input only.\n"
            "Reply with ONLY one JSON object matching this schema, no other text:\n" + schema
            + "\n\nINPUT:\n" + user)


def parse(text):
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return {"answer": text}


def main() -> None:
    EMPTY.mkdir(exist_ok=True)
    packets = {arm: {r["question_id"]: r for r in map(json.loads, (RUN / f"packets-{arm}.jsonl")
                                                    .open(encoding="utf-8"))} for arm in "AB"}
    questions = {r["question_id"]: r for r in stream_instances(DATA)}
    differ = [q for q in packets["A"] if q in packets["B"]
              and packets["A"][q]["passages"] != packets["B"][q]["passages"]]
    out = RUN / "answers.jsonl"
    done = {(r["question_id"], r["arm"]) for r in map(json.loads, out.open(encoding="utf-8"))} \
        if out.exists() else set()
    print(f"{len(packets['A'])} questions, {len(differ)} with different packets", flush=True)
    for qid in differ:
        row = questions[qid]
        for arm in "AB":
            if (qid, arm) in done:
                continue
            result = parse(codex(answer_prompt(row, packets[arm][qid]["passages"])))
            check = judge.get_anscheck_prompt(qid if "_abs" in qid else row["question_type"],
                                              row["question"], str(row["answer"]),
                                              str(result.get("answer")))
            verdict = codex(check + "\n\n(Do not run any commands. Answer yes or no only.)")
            label = "yes" in verdict.lower()
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"question_id": qid, "arm": arm, "type": row["question_type"],
                                    "answer": result.get("answer"), "label": label},
                                   ensure_ascii=False) + "\n")
            print(arm, qid, label, flush=True)
    rows = list(map(json.loads, out.open(encoding="utf-8")))
    label = {(r["question_id"], r["arm"]): r["label"] for r in rows}
    both = [q for q in differ if (q, "A") in label and (q, "B") in label]
    only_b = sum(label[(q, "B")] and not label[(q, "A")] for q in both)
    only_a = sum(label[(q, "A")] and not label[(q, "B")] for q in both)
    n = only_a + only_b
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2 ** n) \
        if n else 1.0
    summary = {"questions": len(packets["A"]), "different_packets": len(differ),
               "answered_both": len(both), "A_right": sum(label[(q, "A")] for q in both),
               "B_right": sum(label[(q, "B")] for q in both), "only_A_right": only_a,
               "only_B_right": only_b, "exact_mcnemar_p": round(p, 3),
               "tokens_median": {arm: sorted(r["estimated_tokens"] for r in packets[arm].values())
                                 [len(packets[arm]) // 2] for arm in "AB"}}
    (RUN / "ab_summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
