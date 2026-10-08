"""Reasoning tokens are 42% of what a session costs to store. Can the effort go down?

Extraction is billed mostly on output (per session: 3.1k input at $0.25/M = 20% of the cost, 1.6k
output at $2/M = 80%), and 640 of those output tokens are reasoning, not events. gpt-5-mini accepts
effort "minimal", so the question is only whether the events survive it. Runs the same sessions
through each effort and reports tokens against what was extracted — fewer tokens are worthless if
fewer facts come back.

    OPENAI_API_KEY=... python reasoning_effort_probe.py
"""
import json
import os
import statistics as st
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from ingestion_gate import GateStatus, classify_event
from longmemeval_ingestion_calibration import payload as ingestion_payload

OUT = Path("runs/graph-v5.5-reasoning-effort")
SRC = Path("runs/graph-v3.8-dev2-k16/sessions.json")
EFFORTS = ("low", "minimal")
N = 30


def summarise(rows, sessions):
    ok = [r for r in rows if r.get("status") == "ok"]
    content = {s["custom_id"]: s["content"] for s in sessions}
    events = [(r["custom_id"], e) for r in ok for e in r["result"]["events"]]
    kept = [e for cid, e in events
            if classify_event(e, content[cid]).status != GateStatus.QUARANTINE]
    usage = [r["usage"] for r in ok]
    inp = st.median(u["input_tokens"] for u in usage)
    out = st.median(u["output_tokens"] for u in usage)
    rea = st.median(u.get("output_tokens_details", {}).get("reasoning_tokens", 0) for u in usage)
    return {"sessions": len(ok), "parse_failures": len(rows) - len(ok),
            "in_tokens": round(inp), "out_tokens": round(out), "reasoning_tokens": round(rea),
            "events_per_session": round(len(events) / max(1, len(ok)), 1),
            "quarantined": len(events) - len(kept),
            "dated_share": round(sum(bool(e.get("date_value") or e.get("effective_time_text"))
                                     for _, e in events) / max(1, len(events)), 3),
            "cost_usd_per_session": round((inp * 0.25 + out * 2.0) / 1e6, 5)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sessions = json.loads(SRC.read_text(encoding="utf-8"))[:N]
    key = os.environ["OPENAI_API_KEY"]
    report = {}
    for effort in EFFORTS:
        path = OUT / f"ingestion-{effort}.jsonl"
        done = pipe.load_jsonl(path, "custom_id")

        def send(row, effort=effort):
            body = ingestion_payload({"content": row["content"]})
            body["reasoning"] = {"effort": effort}
            return pipe.safe_call(key, body, {"custom_id": row["custom_id"]},
                                  lambda raw: json.loads(pipe.response_text(raw)))
        pipe.run_parallel([s for s in sessions if s["custom_id"] not in done], path, 8, send)
        report[effort] = summarise([json.loads(l) for l in path.open(encoding="utf-8")], sessions)

    base, lean = report[EFFORTS[0]], report[EFFORTS[1]]
    report["saving"] = {"output_tokens": f"{1 - lean['out_tokens'] / base['out_tokens']:.0%}",
                        "cost": f"{1 - lean['cost_usd_per_session'] / base['cost_usd_per_session']:.0%}",
                        "events_kept": f"{lean['events_per_session'] / base['events_per_session']:.0%}"}
    (OUT / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
