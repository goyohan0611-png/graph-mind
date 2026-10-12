"""Strict JSON schema forces every field onto every event. Is loosening it worth the risk?

A strict schema requires each property to be listed in `required`, so a field that is empty on 90% of
events is still emitted as an explicit null on all of them — measured at ~185 output tokens per
session after the dead fields were deleted, about 15% of what a session still costs to store. With
strict off, the same schema becomes a hint the model may leave fields out of, which is cheaper and
weaker: adherence is no longer guaranteed, so this measures parse failures and whether the fields that
DO carry values still arrive.

    OPENAI_API_KEY=... python strict_mode_probe.py
"""
import json
import os
import statistics as st
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from longmemeval_ingestion_calibration import payload as ingestion_payload

OUT = Path("runs/graph-v5.7-strict-mode")
SRC = Path("runs/graph-v3.8-dev2-k16/sessions.json")
# What an event cannot be read without. The rest becomes optional: the pipeline already treats every
# one of them as absent-or-empty (`e.get(k) not in (None, "")` when it builds the reader's list).
ESSENTIAL = ["event_local_id", "subject", "relation", "source_turn_ids"]
FIELDS = ("relation_detail", "object_text", "numeric_value", "unit", "date_value",
          "effective_time_text")
N = 30


def body_for(row: dict, strict: bool) -> dict:
    body = ingestion_payload({"content": row["content"]})
    if not strict:
        schema = json.loads(json.dumps(body["text"]["format"]["schema"]))
        schema["properties"]["events"]["items"]["required"] = ESSENTIAL
        body["text"]["format"] = {**body["text"]["format"], "strict": False, "schema": schema}
    return body


def summarise(rows):
    ok = [r for r in rows if r.get("status") == "ok"]
    events = [e for r in ok for e in r["result"]["events"]]
    usage = [r["usage"] for r in ok if r.get("usage")]
    filled = {k: round(sum(1 for e in events if e.get(k) not in (None, "", []))
                       / max(1, len(events)), 2) for k in FIELDS}
    present = {k: round(sum(1 for e in events if k in e) / max(1, len(events)), 2) for k in FIELDS}
    inp = st.median(u["input_tokens"] for u in usage)
    out = st.median(u["output_tokens"] for u in usage)
    return {"sessions": len(ok), "parse_failures": len(rows) - len(ok),
            "in_tokens": round(inp), "out_tokens": round(out),
            "events_per_session": round(len(events) / max(1, len(ok)), 1),
            "key_present_rate": present, "value_filled_rate": filled,
            "cost_usd_per_session": round((inp * .25 + out * 2.) / 1e6, 5)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sessions = json.loads(SRC.read_text(encoding="utf-8"))[:N]
    key = os.environ["OPENAI_API_KEY"]
    report = {}
    for name, strict in (("strict", True), ("loose", False)):
        path = OUT / f"ingestion-{name}.jsonl"
        done = pipe.load_jsonl(path, "custom_id")

        def send(row, strict=strict):
            return pipe.safe_call(key, body_for(row, strict), {"custom_id": row["custom_id"]},
                                  lambda raw: json.loads(pipe.response_text(raw)))
        pipe.run_parallel([s for s in sessions if s["custom_id"] not in done], path, 8, send)
        report[name] = summarise([json.loads(l) for l in path.open(encoding="utf-8")])

    a, b = report["strict"], report["loose"]
    report["saving"] = {"output_tokens": f"{1 - b['out_tokens'] / a['out_tokens']:.0%}",
                        "cost": f"{1 - b['cost_usd_per_session'] / a['cost_usd_per_session']:.0%}",
                        "events_kept": f"{b['events_per_session'] / a['events_per_session']:.0%}"}
    (OUT / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
