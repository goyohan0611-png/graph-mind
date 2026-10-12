"""Question-independent 12-session ingestion calibration for LongMemEval."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tiktoken

from longmemeval_adapter import load_instances
from longmemeval_ingestion_preflight import serialize_session
from memory_vocabulary import RELATIONS


DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
PILOT = Path("runs/graph-v2.1-longmemeval-audit/pilot-manifest.json")
OUTPUT = Path("runs/graph-v2.1-longmemeval-ingestion-calibration-v6")
BASELINE_SELECTION = Path(
    "runs/graph-v2.1-longmemeval-ingestion-calibration-v2/sessions.json")
MODEL = "gpt-5-mini"
PROMPT = """Extract durable long-term memory events from exactly one chat session.
The later question, reference answer, official task type, and evidence labels are unavailable.

Keep explicit user facts, preferences, plans, completed activities, state changes, dates,
quantities, and relationships. From the assistant keep only what a later question could ask it to
repeat: a NAMED recommendation, a specific title, place, product, number or step it gave, or a
commitment it made — at most a few per session, and never its general explanations, caveats or
restatements of what the user just said. Use ASSISTANT_STATED with subject "assistant"; this is
conversation evidence, not an assertion that the claim is externally true. Omit puzzles, quoted
task text, boilerplate, and transient small talk.
Omit conversational reactions such as being glad or happy that the speakers agree unless they
express a durable preference or condition. The subject must be the entity that actually bears the
relation. A commute route is not the user's LOCATED_AT state; use OTHER with a precise relation_detail.

Treat every [user] and [assistant] role label as authoritative. A memory whose subject is the user
must cite at least one [user] turn that directly states that fact. Never turn an [assistant]
first-person statement into a user fact. An assistant-only memory is allowed only for an explicit
recommendation or commitment and must use subject "assistant".

Use the controlled relation list. Use OTHER only when no listed relation fits and provide a short
UPPER_SNAKE_CASE relation_detail. Put the plain object of the relation in object_text, and a numeric
or money amount in numeric_value with its unit. relation_detail only refines the predicate and must
never hold the missing object or value. Split compound quantitative statements into atomic events. For example, "watched
12 films including 5 MCU films" becomes one NUMBER event for all films and another NUMBER event for
MCU films. Do not invent missing dates or units. Every event must cite the TURN_NNN IDs
that directly support it. Return an empty events array when the session has no durable memory.
Put an explicit calendar date in date_value as YYYY-MM-DD, and keep the speaker's own wording for
anything relative ("last weekend", "three weeks ago") in effective_time_text; the engine anchors
those to the session date. Never invent a date.
Return only the requested JSON."""

NULL_STRING = {"type": ["string", "null"]}
NULL_NUMBER = {"type": ["number", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "event_local_id": {"type": "string"},
                "subject": {"type": "string"},
                "relation": {"type": "string", "enum": list(RELATIONS)},
                "relation_detail": NULL_STRING,
                "object_text": NULL_STRING,
                "numeric_value": NULL_NUMBER,
                "unit": NULL_STRING,
                "date_value": NULL_STRING,
                "effective_time_text": NULL_STRING,
                "source_turn_ids": {"type": "array", "items": {"type": "string"}},
            },
            # Strict JSON schema makes every property required, so an unread field is not free: it is
            # emitted as an explicit null on every event. Measured on 209 dev2 events, confidence,
            # action, value_type, numeric_min, numeric_max and boolean_value cost 35 of the 111
            # tokens an event takes and had zero readers in the retrieval or answer path
            # (ingestion_gate only used them for an EVIDENCE_ONLY status the executor ignores).
            "required": ["event_local_id", "subject", "relation", "relation_detail",
                         "object_text", "numeric_value", "unit", "date_value",
                         "effective_time_text", "source_turn_ids"],
            "additionalProperties": False,
        }},
    },
    "required": ["events"],
    "additionalProperties": False,
}


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def response_text(response: dict) -> str:
    pieces = [piece for item in response.get("output", [])
              if item.get("type") == "message" for piece in item.get("content", [])]
    if any(piece.get("type") == "refusal" for piece in pieces):
        raise ValueError("Model refusal")
    return "".join(piece["text"] for piece in pieces
                   if piece.get("type") == "output_text")


def bad_role_provenance(events: list[dict], content: str) -> list[str]:
    """Return user-memory event IDs unsupported by any user-authored source turn."""
    turn_roles = {line.split(" ", 1)[0]:
                  ("user" if "[user]:" in line else "assistant")
                  for line in content.splitlines() if line.startswith("TURN_")}
    invalid = []
    for event in events:
        source_roles = {turn_roles.get(source) for source in event["source_turn_ids"]}
        if (event["subject"].strip().lower() in {"user", "the user"}
                and "user" not in source_roles):
            invalid.append(event["event_local_id"])
    return invalid


def role_played_user_turn_ids(content: str) -> set[str]:
    """Find assistant-channel turns explicitly requested to role-play the user."""
    role_play = False
    result = set()
    for line in content.splitlines():
        if not line.startswith("TURN_"):
            continue
        turn_id = line.split(" ", 1)[0]
        lowered = line.lower()
        if "[user]:" in lowered and "respond as the user" in lowered:
            role_play = True
            continue
        if role_play and "[assistant]:" in lowered:
            result.add(turn_id)
    return result


def candidate_sessions() -> list[dict]:
    selected = set(json.loads(PILOT.read_text(encoding="utf-8"))["question_ids"])
    encoding = tiktoken.encoding_for_model(MODEL)
    candidates = []
    for instance in load_instances(DATA):
        if instance["question_id"] not in selected:
            continue
        for date, session_id, turns in zip(instance["haystack_dates"],
                                           instance["haystack_session_ids"],
                                           instance["haystack_sessions"]):
            content = serialize_session(date, session_id, turns)
            candidates.append({"instance_id": instance["question_id"],
                               "session_id": session_id, "date": date,
                               "content": content,
                               "input_tokens": len(encoding.encode(PROMPT))
                                               + len(encoding.encode(content)) + 12})
    candidates.sort(key=lambda row: (row["input_tokens"],
                                     digest((row["instance_id"], row["session_id"]))))
    return candidates


def select_calibration() -> list[dict]:
    rows = candidate_sessions()
    by_id = {(row["instance_id"], row["session_id"]): row for row in rows}
    if BASELINE_SELECTION.exists():
        baseline = json.loads(BASELINE_SELECTION.read_text(encoding="utf-8"))
        return [{**by_id[(item["instance_id"], item["session_id"])],
                 "length_band": item["length_band"]} for item in baseline]
    groups = (("short", rows[:len(rows) // 3]),
              ("medium", rows[len(rows) // 3:2 * len(rows) // 3]),
              ("long", rows[2 * len(rows) // 3:]))
    selected = []
    for label, group in groups:
        ordered = sorted(group, key=lambda row: digest(
            (row["instance_id"], row["session_id"])))
        selected.extend({**row, "length_band": label} for row in ordered[:4])
    return selected


def prepare() -> list[dict]:
    rows = select_calibration()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    public = [{k: row[k] for k in ("instance_id", "session_id", "date", "content",
                                    "input_tokens", "length_band")} for row in rows]
    freeze = {"role": "question-independent ingestion schema calibration",
              "model": MODEL, "sessions": len(public),
              "selection": "same 12 session IDs and bands frozen by v2 calibration",
              "selection_parent": str(BASELINE_SELECTION),
              "session_sha256": digest(public), "prompt_sha256": digest(PROMPT),
              "schema_sha256": digest(SCHEMA),
              "model_input_excludes": ["question", "answer", "answer_session_ids",
                                       "has_answer", "official_question_type"]}
    (OUTPUT / "sessions.json").write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    return public


def payload(row: dict, max_output_tokens: int = 8192) -> dict:
    # 4096 truncated the JSON on long sessions (11 of 2211 on the k=16 dev2 run), losing every event
    # in that session; reasoning tokens share this budget, so the event list gets what is left.
    return {"model": MODEL, "store": False,
            "input": [{"role": "system", "content": PROMPT},
                      {"role": "user", "content": row["content"]}],
            "reasoning": {"effort": "low"},
            "text": {"format": {"type": "json_schema", "name": "memory_events",
                                  "strict": True, "schema": SCHEMA}},
            "max_output_tokens": max_output_tokens}


def read_records() -> dict[str, dict]:
    path = OUTPUT / "responses.jsonl"
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            result[row["custom_id"]] = row
    return result


def run(key: str, workers: int) -> None:
    rows = prepare()
    previous = read_records()

    def send(row: dict) -> dict:
        custom_id = f"{row['instance_id']}::{row['session_id']}"
        record = {"custom_id": custom_id, "length_band": row["length_band"],
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        started = time.perf_counter()
        try:
            request = Request("https://api.openai.com/v1/responses",
                              data=json.dumps(payload(row)).encode("utf-8"),
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"})
            with urlopen(request, timeout=120) as response:
                raw = json.load(response)
            text = response_text(raw)
            parsed = json.loads(text)
            valid_turns = {line.split(" ", 1)[0] for line in row["content"].splitlines()
                           if line.startswith("TURN_")}
            bad_sources = sorted({source for event in parsed["events"]
                                  for source in event["source_turn_ids"]
                                  if source not in valid_turns})
            bad_role_events = bad_role_provenance(parsed["events"], row["content"])
            record.update(status="ok", events=parsed["events"], bad_source_turn_ids=bad_sources,
                          bad_role_provenance_event_ids=bad_role_events,
                          usage=raw.get("usage", {}), response_id=raw.get("id"),
                          model=raw.get("model"))
        except HTTPError as error:
            code = ""
            try:
                code = json.loads(error.read()).get("error", {}).get("code", "")
            except (ValueError, AttributeError):
                pass
            record.update(status="transport_error", http_status=error.code,
                          api_error_code=code)
        except (URLError, TimeoutError) as error:
            record.update(status="transport_error", error=type(error).__name__)
        except (ValueError, KeyError, TypeError) as error:
            # The response may be incomplete because reasoning and visible output
            # share max_output_tokens. Keep billing/status metadata without storing
            # the raw natural-language response.
            details = locals().get("raw", {})
            record.update(status="parse_failure", error=str(error),
                          usage=details.get("usage", {}),
                          response_id=details.get("id"),
                          model=details.get("model"),
                          response_status=details.get("status"),
                          incomplete_details=details.get("incomplete_details"),
                          output_text_chars=len(locals().get("text", "")))
        record["seconds"] = time.perf_counter() - started
        return record

    pending = [row for row in rows
               if f"{row['instance_id']}::{row['session_id']}" not in previous]
    with (OUTPUT / "responses.jsonl").open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(send, row) for row in pending]
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                print(f"{record['custom_id']}: {record['status']}", flush=True)
    summarize()


def summarize() -> dict:
    rows = prepare()
    records = read_records()
    ok = [record for record in records.values() if record["status"] == "ok"]
    source_rows = {f"{row['instance_id']}::{row['session_id']}": row for row in rows}
    role_played_event_count = 0
    unexplained_role_event_count = 0
    for record in ok:
        invalid = set(record.get("bad_role_provenance_event_ids", []))
        role_played_turns = role_played_user_turn_ids(source_rows[record["custom_id"]]["content"])
        role_played = {event["event_local_id"] for event in record["events"]
                       if event["event_local_id"] in invalid
                       and set(event["source_turn_ids"]) <= role_played_turns}
        role_played_event_count += len(role_played)
        unexplained_role_event_count += len(invalid - role_played)
    event_counts = [len(record["events"]) for record in ok]
    billed = [record for record in records.values() if record.get("usage")]
    input_tokens = sum(record.get("usage", {}).get("input_tokens", 0) for record in billed)
    output_tokens = sum(record.get("usage", {}).get("output_tokens", 0) for record in billed)
    report = {"role": "question-independent ingestion calibration", "model": MODEL,
              "sessions": len(rows), "successful": len(ok),
              "failed_or_missing": len(rows) - len(ok),
              "events_total": sum(event_counts),
              "events_per_session_median": statistics.median(event_counts) if event_counts else None,
              "empty_sessions": sum(count == 0 for count in event_counts),
              "invalid_provenance_sessions": sum(bool(row.get("bad_source_turn_ids")) for row in ok),
              "invalid_role_provenance_events": sum(
                  len(row.get("bad_role_provenance_event_ids", [])) for row in ok),
              "invalid_role_provenance_sessions": sum(
                  bool(row.get("bad_role_provenance_event_ids")) for row in ok),
              "role_played_user_events_quarantined": role_played_event_count,
              "unexplained_role_provenance_events": unexplained_role_event_count,
              "accepted_events_after_role_quarantine": (
                  sum(event_counts) - role_played_event_count - unexplained_role_event_count),
              "billed_responses": len(billed),
              "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                        "standard_cost_usd": input_tokens / 1e6 * .25 + output_tokens / 1e6 * 2},
              "api_calls": len(records)}
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# LongMemEval 12-session ingestion calibration", "",
             "> 질문·정답·evidence label 없이 session만 입력한다.", "",
             f"- 성공: **{len(ok)}/{len(rows)}**",
             f"- 추출 event: **{sum(event_counts)}개**",
             f"- 빈 session: **{report['empty_sessions']}개**",
             f"- 잘못된 source turn ID가 있는 session: **{report['invalid_provenance_sessions']}개**",
             f"- assistant 발화를 user 기억으로 오귀속한 event: **{report['invalid_role_provenance_events']}개**",
             f"- 명시적 user role-play로 판정해 격리한 event: **{report['role_played_user_events_quarantined']}개**",
             f"- 설명되지 않은 role 오귀속: **{report['unexplained_role_provenance_events']}개**",
             f"- role 검증 후 저장 가능한 event: **{report['accepted_events_after_role_quarantine']}개**",
             f"- 실제 token: input **{input_tokens:,}**, output **{output_tokens:,}**, 비용 **${report['usage']['standard_cost_usd']:.4f}**", "",
             "성공 후 event 수, provenance와 output token을 바탕으로 2,886-session Batch 여부를 결정한다."]
    (OUTPUT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    prepare()
    if args.run:
        key = (getpass.getpass("OpenAI API key: ") if args.key_stdin
               else os.environ.get("OPENAI_API_KEY", ""))
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent; no requests sent")
        run(key, args.workers)
    else:
        summarize()


if __name__ == "__main__":
    main()
