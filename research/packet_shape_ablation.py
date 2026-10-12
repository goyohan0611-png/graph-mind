"""How long and how many should the evidence spans be? Offline, no API, no noise.

The reader currently gets up to 15 source turns of up to 1000 characters each. Both numbers were
picked, not measured. Evidence coverage — the share of answer-bearing turns that actually reach the
packet — is deterministic, so the trade-off between tokens and coverage can be mapped exactly before
spending anything on accuracy runs.

Reports, for each (turns, chars) pair: coverage, and the characters the packet costs.

Read the chars axis with care: the hit test below looks for a marker taken from characters 40-120 of
the turn, so it survives every cap tried here and the coverage column is flat along chars by
construction. This measures how MANY spans to send. How LONG they may be cut is span_cap_check.py.

    python packet_shape_ablation.py
"""
from pathlib import Path
import json

import longmemeval_retrieval_executor as pipe
from longmemeval_adapter import load_instances

RUN = Path("runs/graph-v3.8-dev2-k16")
IDS = Path("runs/graph-v3.2-untouched-eval-v2")
OUT = Path("runs/graph-v5.3-packet-shape")
TURNS = (8, 12, 15, 20)
CHARS = (300, 500, 800, 1000)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pipe.OUTPUT = RUN
    ids = set(json.loads((IDS / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    questions = json.loads((RUN / "questions.json").read_text(encoding="utf-8"))
    sessions = json.loads((RUN / "sessions.json").read_text(encoding="utf-8"))
    ingested = pipe.load_jsonl(RUN / "ingestion.jsonl", "custom_id")
    plans = {json.loads(l)["question_id"]: (json.loads(l).get("plan") or {})
             for l in (RUN / "plans.jsonl").open(encoding="utf-8")}
    raw = {r["question_id"]: r for r in load_instances(pipe.DATA) if r["question_id"] in ids}

    cases = {}
    for question in questions:
        qid = question["question_id"]
        have = {s["session_id"] for s in sessions if s["question_id"] == qid}
        wanted = {}
        for sid, turns in zip(raw[qid]["haystack_session_ids"], raw[qid]["haystack_sessions"]):
            if sid not in have:
                continue
            for i, turn in enumerate(turns):
                if turn.get("has_answer"):
                    wanted[(sid, f"TURN_{i:03d}")] = turn["content"]
        if not wanted:
            continue
        events = pipe.accepted_events(qid, sessions, ingested,
                                      include_assistant=pipe.asks_assistant_history(question["question"]))
        # Build each shape the way the pipeline does. evidence_passages returns spans in DATE order,
        # so slicing its output would keep the oldest spans rather than the most relevant ones.
        cases[qid] = (wanted, {turns: pipe.evidence_passages(question, sessions, events,
                                                             plans.get(qid), top_events=turns,
                                                             turn_cap=4000)
                               for turns in TURNS})

    report = {}
    for turns in TURNS:
        for chars in CHARS:
            hit = total = cost = 0
            for wanted, packets in cases.values():
                kept = packets[turns]
                cost += sum(min(len(p["text"]), chars) for p in kept)
                for (sid, tid), text in wanted.items():
                    span = next((p for p in kept
                                 if p["session_id"] == sid and p.get("turn_id") == tid), None)
                    # the answer only counts as delivered if the truncation kept the sentence
                    marker = text[40:120] if len(text) > 120 else text[:60]
                    hit += bool(span and marker in span["text"][:chars])
                    total += 1
            report[f"{turns}x{chars}"] = {"coverage": round(hit / total, 4),
                                          "chars_per_question": round(cost / len(cases)),
                                          "approx_tokens": round(cost / len(cases) / 4)}
    (OUT / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    baseline = report["15x1000"]
    print(f"{'shape':>10} {'coverage':>9} {'tokens':>8}   vs current")
    for name, row in report.items():
        mark = "  <= current" if name == "15x1000" else ""
        print(f"{name:>10} {row['coverage']*100:8.1f}% {row['approx_tokens']:8d}"
              f"   {row['coverage']-baseline['coverage']:+.3f} cov"
              f" {row['approx_tokens']-baseline['approx_tokens']:+5d} tok{mark}")


if __name__ == "__main__":
    main()
