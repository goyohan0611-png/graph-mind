"""Does reasoning effort "minimal" still find the facts? Coverage first, accuracy only if it holds.

reasoning_effort_probe.py showed minimal costs 54% less per session but extracts 27% fewer events.
Fewer events is only cheaper if the ones that mattered are still there, and that is decidable offline:
evidence coverage is the share of answer-bearing source turns that actually reach the reader's packet,
and it is deterministic. Both efforts ingest the same 20-question haystacks, then coverage is computed
for each. Only a winner gets the answer and judge calls.

    OPENAI_API_KEY=... python effort_coverage_check.py
"""
import json
import os
import shutil
import statistics as st
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from longmemeval_adapter import load_instances
from longmemeval_ingestion_calibration import payload as ingestion_payload

SRC = Path("runs/graph-v5.2-trim-check")     # same questions, sessions, plans and embed cache
EFFORTS = ("low", "minimal")


def run_dir(effort: str) -> Path:
    out = Path(f"runs/graph-v5.6-effort-{effort}")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("questions.json", "sessions.json", "plans.jsonl"):
        if not (out / name).exists():
            shutil.copyfile(SRC / name, out / name)
    if not (out / "embed-cache-local.vec").exists():
        shutil.copytree(SRC / "embed-cache-local.vec", out / "embed-cache-local.vec")
    return out


def coverage(out: Path, questions, sessions, plans, gold) -> dict:
    """Share of answer-bearing turns whose text reaches the evidence packet."""
    pipe.OUTPUT = out
    ingested = pipe.load_jsonl(out / "ingestion.jsonl", "custom_id")
    hit = total = 0
    for question in questions:
        qid = question["question_id"]
        have = {s["session_id"] for s in sessions if s["question_id"] == qid}
        wanted = {}
        for sid, turns in zip(gold[qid]["haystack_session_ids"], gold[qid]["haystack_sessions"]):
            if sid not in have:
                continue
            for i, turn in enumerate(turns):
                if turn.get("has_answer"):
                    wanted[(sid, f"TURN_{i:03d}")] = turn["content"]
        if not wanted:
            continue
        plan = plans.get(qid) or {}
        events = pipe.accepted_events(qid, sessions, ingested,
                                      include_assistant=pipe.wants_assistant_turns(
                                          question["question"], plan))
        packet = pipe.evidence_passages(question, sessions, events, plan)
        for (sid, tid), text in wanted.items():
            span = next((p for p in packet
                         if p["session_id"] == sid and p.get("turn_id") == tid), None)
            marker = text[40:120] if len(text) > 120 else text[:60]
            hit += bool(span and marker in span["text"])
            total += 1
    rows = [r for r in ingested.values() if r.get("status") == "ok" and r.get("usage")]
    events = sum(len(r["result"]["events"]) for r in rows)
    return {"coverage": round(hit / total, 4), "answer_turns": total, "reached": hit,
            "sessions": len(rows), "events_per_session": round(events / max(1, len(rows)), 1),
            "out_tokens": round(st.median(r["usage"]["output_tokens"] for r in rows)),
            "cost_usd": round(sum(r["usage"]["input_tokens"] * .25
                                  + r["usage"]["output_tokens"] * 2. for r in rows) / 1e6, 4)}


def main():
    key = os.environ["OPENAI_API_KEY"]
    questions = json.loads((SRC / "questions.json").read_text(encoding="utf-8"))
    sessions = json.loads((SRC / "sessions.json").read_text(encoding="utf-8"))
    plans = {json.loads(l)["question_id"]: (json.loads(l).get("plan") or {})
             for l in (SRC / "plans.jsonl").open(encoding="utf-8")}
    ids = {q["question_id"] for q in questions}
    gold = {r["question_id"]: r for r in load_instances(pipe.DATA) if r["question_id"] in ids}

    report = {}
    for effort in EFFORTS:
        out = run_dir(effort)
        path = out / "ingestion.jsonl"
        done = pipe.load_jsonl(path, "custom_id")

        def send(row, effort=effort):
            body = ingestion_payload({"content": row["content"]})
            body["reasoning"] = {"effort": effort}
            return pipe.safe_call(key, body, {"custom_id": row["custom_id"]},
                                  lambda raw: json.loads(pipe.response_text(raw)))
        pending = [s for s in sessions if s["custom_id"] not in done]
        print(f"--- {effort}: {len(pending)} sessions to ingest", flush=True)
        pipe.run_parallel(pending, path, 8, send)
        report[effort] = coverage(out, questions, sessions, plans, gold)
        pipe._EMBEDDER and pipe._EMBEDDER.flush()

    base, lean = report[EFFORTS[0]], report[EFFORTS[1]]
    report["verdict"] = {
        "coverage_delta": round(lean["coverage"] - base["coverage"], 4),
        "cost_saving": f"{1 - lean['cost_usd'] / base['cost_usd']:.0%}",
        "spend_answer_calls": lean["coverage"] >= base["coverage"] - 0.01}
    Path("runs/graph-v5.6-effort-low/report.json").write_text(json.dumps(report, indent=1),
                                                             encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
