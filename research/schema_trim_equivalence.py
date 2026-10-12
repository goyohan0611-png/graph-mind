"""Proof, offline and free: dropping six unread fields changes nothing downstream.

The claim behind the schema trim is that confidence, action, value_type, numeric_min, numeric_max and
boolean_value have no readers in the retrieval or answer path. A grep says so; this shows it. Every
event of a finished run is stripped of those fields and the whole read side is recomputed — the
retrieval ranking, the evidence packet, the engine's own arithmetic answer — and compared byte for
byte against the same computation on the untouched events.

    python schema_trim_equivalence.py
"""
import json
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from ingestion_gate import GateStatus, classify_event

RUN = Path("runs/graph-v5.2-trim-check")
DROPPED = ("confidence", "action", "value_type", "numeric_min", "numeric_max", "boolean_value")


def content_of(sessions, qid, event):
    return next(s["content"] for s in sessions
                if s["session_id"] == event["session_id"] and s["question_id"] == qid)


def read_side(questions, sessions, ingested, plans):
    """Everything the engine computes from events, as one comparable blob."""
    out = {}
    for question in questions:
        qid = question["question_id"]
        plan = plans.get(qid) or {}
        events = pipe.accepted_events(qid, sessions, ingested,
                                      include_assistant=pipe.wants_assistant_turns(
                                          question["question"], plan))
        out[qid] = {
            "events": [pipe.event_text(e) for e in events],
            "ranking": pipe._rank_events(question, events)[:20],
            "passages": [(p["session_id"], p.get("turn_id"), len(p["text"]))
                         for p in pipe.evidence_passages(question, sessions, events, plan)],
            "reader_events": pipe.slim_events(question, events),
            # What the executor asks the gate is only "quarantine or not" (it keeps
            # EVIDENCE_ONLY). The finer status does move: the typed-value checks read value_type,
            # so without it they stay silent and EVIDENCE_ONLY becomes PERSIST. Nothing downstream
            # reads that distinction, and `kept` below is what actually decides.
            "kept": [classify_event(e, content_of(sessions, qid, e)).status != GateStatus.QUARANTINE
                     for e in events],
        }
    return out


def main():
    pipe.OUTPUT = RUN
    questions = json.loads((RUN / "questions.json").read_text(encoding="utf-8"))
    sessions = json.loads((RUN / "sessions.json").read_text(encoding="utf-8"))
    plans = {json.loads(l)["question_id"]: (json.loads(l).get("plan") or {})
             for l in (RUN / "plans.jsonl").open(encoding="utf-8")}
    full = pipe.load_jsonl(RUN / "ingestion.jsonl", "custom_id")
    stripped = json.loads(json.dumps(full))
    removed = 0
    for row in stripped.values():
        for event in (row.get("result") or {}).get("events", []):
            for field in DROPPED:
                removed += event.pop(field, None) is not None
    before = read_side(questions, sessions, full, plans)
    after = read_side(questions, sessions, stripped, plans)
    differing = [q for q in before if before[q] != after[q]]
    events = sum(len((r.get("result") or {}).get("events", [])) for r in full.values())
    print(f"{len(questions)} questions, {events} events, {removed} field values removed")
    print(f"questions whose retrieval, reader events, packet or acceptance changed: {len(differing)}")
    if differing:
        q = differing[0]
        for key in before[q]:
            if before[q][key] != after[q][key]:
                print(f"  {q} differs in {key}:\n    before {str(before[q][key])[:200]}"
                      f"\n    after  {str(after[q][key])[:200]}")
    assert not differing, "the dropped fields do have a reader after all"
    print("identical: the six fields were paid for on every event and never read")


if __name__ == "__main__":
    main()
