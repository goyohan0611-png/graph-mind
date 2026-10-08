"""Targeted v7 calibration for entity type, aliases, categories, and normalized time."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ingestion_gate import GateStatus, classify_event
from longmemeval_adapter import load_instances
from longmemeval_ingestion_calibration import MODEL, RELATIONS, digest, response_text
from longmemeval_ingestion_preflight import serialize_session
from memory_vocabulary import ENTITY_TYPES
from memory_normalization import normalize as normalized


DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
RETRIEVAL_RUNTIME = Path("runs/graph-v2.1-longmemeval-retrieval-pilot/runtime.json")
OUTPUT = Path("runs/graph-v2.1-longmemeval-ingestion-v7")
TARGETS = (
    ("80ec1f4f_abs", "answer_990c8992_abs_1"),
    ("80ec1f4f_abs", "answer_990c8992_abs_2"),
    ("80ec1f4f_abs", "answer_990c8992_abs_3"),
    ("031748ae_abs", "answer_8748f791_abs_1"),
    ("031748ae_abs", "answer_8748f791_abs_2"),
)
TIME_GRANULARITIES = ("INSTANT", "DAY", "MONTH", "YEAR", "INTERVAL", "RELATIVE", "UNKNOWN")

PROMPT = """Extract durable long-term-memory events from exactly one chat session.
The later question, answer, task type, and evidence labels are unavailable.

Keep explicit user facts, preferences, plans, completed activities, state changes, dates,
quantities, relationships, and salient assistant statements. Treat role labels as authoritative.
User facts require a directly supporting user turn. Assistant-only claims use subject assistant and
ASSISTANT_STATED. Omit generic knowledge, boilerplate, puzzles, and transient small talk.

Preserve both the raw semantic relation and an executor relation from the controlled list. For each
object, preserve its canonical surface name, explicitly stated aliases, entity type, and explicitly
stated category words. category_tags must be uppercase singular forms grounded in cited source text;
for example, a named venue explicitly called a gallery gets GALLERY, a museum gets MUSEUM, and a job
title gets entity type ROLE. Do not infer a category from outside knowledge. An alias must occur in a
cited turn and must not be an invented abbreviation.

Normalize explicit time into ISO time_start/time_end where possible and record its granularity.
Keep the original effective_time_text. Do not invent missing year, date, category, alias, or unit.
Split compound quantities into atomic events. Every event must cite its supporting TURN_NNN IDs.
Return only the requested JSON."""

NULL_STRING = {"type": ["string", "null"]}
NULL_NUMBER = {"type": ["number", "null"]}
SCHEMA = {"type": "object", "properties": {"events": {"type": "array", "items": {
    "type": "object", "properties": {
        "event_local_id": {"type": "string"}, "subject": {"type": "string"},
        "relation": {"type": "string", "enum": list(RELATIONS)},
        "raw_relation_text": NULL_STRING, "relation_detail": NULL_STRING,
        "object_text": NULL_STRING, "object_canonical_name": NULL_STRING,
        "object_type": {"type": "string", "enum": list(ENTITY_TYPES)},
        "object_aliases": {"type": "array", "items": {"type": "string"}},
        "category_tags": {"type": "array", "items": {"type": "string"}},
        "value_type": {"type": "string", "enum": [
            "ENTITY", "TEXT", "NUMBER", "MONEY", "DURATION", "DATE", "BOOLEAN"]},
        "numeric_value": NULL_NUMBER, "numeric_min": NULL_NUMBER, "numeric_max": NULL_NUMBER,
        "boolean_value": {"type": ["boolean", "null"]}, "unit": NULL_STRING,
        "date_value": NULL_STRING, "effective_time_text": NULL_STRING,
        "time_start": NULL_STRING, "time_end": NULL_STRING,
        "time_granularity": {"type": "string", "enum": list(TIME_GRANULARITIES)},
        "action": {"type": "string", "enum": ["ASSERT", "RETRACT"]},
        "source_turn_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"}},
    "required": ["event_local_id", "subject", "relation", "raw_relation_text",
        "relation_detail", "object_text", "object_canonical_name", "object_type",
        "object_aliases", "category_tags", "value_type", "numeric_value", "numeric_min",
        "numeric_max", "boolean_value", "unit", "date_value", "effective_time_text",
        "time_start", "time_end", "time_granularity", "action", "source_turn_ids", "confidence"],
    "additionalProperties": False}}}, "required": ["events"], "additionalProperties": False}


def deterministic_entity_id(event: dict) -> str | None:
    name = normalized(event.get("object_canonical_name") or event.get("object_text"))
    if not name or event.get("object_type") == "NONE":
        return None
    identity = f"{event.get('object_type', 'OTHER')}::{name}"
    return "entity_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def turn_texts(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current = None
    for line in content.splitlines():
        if line.startswith("TURN_") and "]: " in line:
            current, rest = line.split(" ", 1)
            result[current] = rest.split("]: ", 1)[1]
        elif current:
            result[current] += "\n" + line
    return result


def sanitize_enrichment(event: dict, content: str) -> tuple[dict, list[str], list[str]]:
    """Keep only source-grounded aliases/tags and attach category provenance."""
    event = dict(event)
    turns = turn_texts(content)
    cited_text = "\n".join(turns.get(turn_id, "") for turn_id in event["source_turn_ids"])
    removed_aliases = [alias for alias in event["object_aliases"]
                       if normalized(alias) not in normalized(cited_text)]
    event["object_aliases"] = [alias for alias in event["object_aliases"]
                               if normalized(alias) in normalized(cited_text)]
    object_name = normalized(event.get("object_canonical_name") or event.get("object_text"))
    original_tags = list(event["category_tags"])
    kept_tags = []
    category_sources: dict[str, list[str]] = {}
    for tag in event["category_tags"]:
        term = normalized(tag.replace("_", " "))
        sources = []
        for turn_id, text in turns.items():
            text_norm = normalized(text)
            tag_present = term in text_norm or f"{term}s" in text_norm
            same_entity = not object_name or object_name in text_norm
            if tag_present and (turn_id in event["source_turn_ids"] or same_entity):
                sources.append(turn_id)
        if sources:
            kept_tags.append(tag)
            category_sources[tag] = sources
    event["category_tags"] = kept_tags
    event["category_source_turn_ids"] = category_sources
    event["canonical_entity_id"] = deterministic_entity_id(event)
    removed_tags = [tag for tag in original_tags if tag not in kept_tags]
    return event, removed_aliases, removed_tags


def all_sessions() -> dict[tuple[str, str], dict]:
    result = {}
    for row in load_instances(DATA):
        for date, session_id, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                           row["haystack_sessions"]):
            result[(row["question_id"], session_id)] = {
                "question_id": row["question_id"], "session_id": session_id, "date": date,
                "content": serialize_session(date, session_id, turns)}
    return result


def select_sessions() -> list[dict]:
    universe = all_sessions()
    selected = [{**universe[key], "selection_role": "targeted_failure_case"} for key in TARGETS]
    runtime = json.loads(RETRIEVAL_RUNTIME.read_text(encoding="utf-8"))
    candidates = []
    excluded = set(TARGETS)
    for row in runtime:
        for session in row["sessions"]:
            key = (row["question_id"], session["session_id"])
            if key not in excluded:
                candidates.append({**universe[key], "selection_role": "hash_diversity"})
    candidates.sort(key=lambda row: digest((row["question_id"], row["session_id"])))
    selected.extend(candidates[:7])
    return selected


def payload(content: str) -> dict:
    return {"model": MODEL, "store": False, "reasoning": {"effort": "low"},
            "input": [{"role": "system", "content": PROMPT},
                      {"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": "memory_ingestion_v7",
                                 "strict": True, "schema": SCHEMA}},
            "max_output_tokens": 8192}


def call_api(key: str, body: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    request = Request("https://api.openai.com/v1/responses",
                      data=json.dumps(body).encode("utf-8"),
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=180) as response:
        return json.load(response), time.perf_counter() - started


def load_responses() -> dict[str, dict]:
    result = {}
    path = OUTPUT / "responses.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                result[row["custom_id"]] = row
    return result


def send(key: str, row: dict) -> dict:
    body = payload(row["content"])
    record = {"custom_id": f"{row['question_id']}::{row['session_id']}",
              "question_id": row["question_id"], "session_id": row["session_id"],
              "request_sha256": digest(body), "timestamp": datetime.now(timezone.utc).isoformat()}
    started = time.perf_counter()
    try:
        raw, seconds = call_api(key, body)
        result = json.loads(response_text(raw))
        for event in result["events"]:
            event["canonical_entity_id"] = deterministic_entity_id(event)
        record.update(status="ok", result=result, usage=raw.get("usage", {}),
                      response_id=raw.get("id"), model=raw.get("model"), seconds=seconds)
    except HTTPError as error:
        record.update(status="transport_error", http_status=error.code,
                      seconds=time.perf_counter() - started)
    except (URLError, TimeoutError) as error:
        record.update(status="transport_error", error=type(error).__name__,
                      seconds=time.perf_counter() - started)
    except (ValueError, KeyError, TypeError) as error:
        record.update(status="parse_failure", error=str(error),
                      usage=locals().get("raw", {}).get("usage", {}),
                      seconds=time.perf_counter() - started)
    return record


def run(key: str, workers: int, sessions: list[dict]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "responses.jsonl"
    previous = load_responses()
    pending = [row for row in sessions if previous.get(
        f"{row['question_id']}::{row['session_id']}", {}).get("status") != "ok"]
    with path.open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(send, key, row) for row in pending]
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                print(f"{record['custom_id']} {record['status']}", flush=True)


EXPECTED = {
    "80ec1f4f_abs::answer_990c8992_abs_1": ("Natural History Museum", "PLACE", "MUSEUM"),
    "80ec1f4f_abs::answer_990c8992_abs_2": ("The Art Cube", "PLACE", "GALLERY"),
    "80ec1f4f_abs::answer_990c8992_abs_3": ("Modern Art Museum", "PLACE", "MUSEUM"),
    "031748ae_abs::answer_8748f791_abs_1": ("Senior Software Engineer", "ROLE", None),
    "031748ae_abs::answer_8748f791_abs_2": ("Senior Software Engineer", "ROLE", None),
}


def audit(sessions: list[dict]) -> dict:
    responses = load_responses()
    session_map = {f"{row['question_id']}::{row['session_id']}": row for row in sessions}
    target_results = []
    removed_unsupported_aliases = []
    removed_unsupported_tags = []
    unsupported_persisted_aliases = []
    invalid_sources = []
    gate_counts = {status.value: 0 for status in GateStatus}
    total_events = 0
    persisted_rows = []
    for custom_id, record in responses.items():
        if custom_id not in session_map or record.get("status") != "ok":
            continue
        source = session_map[custom_id]["content"]
        source_norm = normalized(source)
        total_events += len(record["result"]["events"])
        sanitized_events = []
        for raw_event in record["result"]["events"]:
            event, removed, removed_tags = sanitize_enrichment(raw_event, source)
            sanitized_events.append(event)
            removed_unsupported_aliases.extend(
                {"event": f"{custom_id}::{event['event_local_id']}", "alias": alias}
                for alias in removed)
            removed_unsupported_tags.extend(
                {"event": f"{custom_id}::{event['event_local_id']}", "tag": tag}
                for tag in removed_tags)
            decision = classify_event(event, source)
            gate_counts[decision.status.value] += 1
            if decision.status == GateStatus.PERSIST:
                persisted_rows.append({"custom_id": custom_id,
                                       "event": event, "gate_status": decision.status.value})
            if "UNKNOWN_SOURCE" in decision.reasons:
                invalid_sources.append(f"{custom_id}::{event['event_local_id']}")
            for alias in event["object_aliases"]:
                if normalized(alias) not in source_norm:
                    unsupported_persisted_aliases.append({
                        "event": f"{custom_id}::{event['event_local_id']}", "alias": alias})
        if custom_id in EXPECTED:
            name, entity_type, tag = EXPECTED[custom_id]
            matches = [event for event in sanitized_events if normalized(name) in normalized(
                " ".join([str(event.get("object_text") or ""),
                          str(event.get("object_canonical_name") or ""),
                          " ".join(event.get("object_aliases", []))]))]
            passed = any(event["object_type"] == entity_type and (
                tag is None or tag in event["category_tags"]) for event in matches)
            target_results.append({"custom_id": custom_id, "expected_name": name,
                                   "expected_type": entity_type, "expected_tag": tag,
                                   "matching_events": len(matches), "passed": passed})
    ok = [row for row in responses.values() if row.get("status") == "ok"]
    inp = sum(row.get("usage", {}).get("input_tokens", 0) for row in responses.values())
    out = sum(row.get("usage", {}).get("output_tokens", 0) for row in responses.values())
    report = {"role": "targeted v7 entity enrichment development calibration",
              "sessions": len(sessions), "success": len(ok), "events": total_events,
              "target_checks_passed": sum(row["passed"] for row in target_results),
              "target_checks_total": len(target_results), "target_results": target_results,
              "removed_unsupported_aliases": removed_unsupported_aliases,
              "removed_unsupported_tags": removed_unsupported_tags,
              "unsupported_persisted_aliases": unsupported_persisted_aliases,
              "invalid_sources": invalid_sources,
              "gate_counts": gate_counts,
              "usage": {"input_tokens": inp, "output_tokens": out,
                        "cost_usd": inp / 1e6 * .25 + out / 1e6 * 2},
              "claim_limit": "Targeted after observed failures; not blind or general ingestion accuracy."}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "persisted.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in persisted_rows),
        encoding="utf-8")
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# LongMemEval ingestion v7 targeted calibration", "",
             "> 관측된 category/role 손실 뒤에 만든 development diagnostic이다.", "",
             f"- API 성공: **{len(ok)}/{len(sessions)}**",
             f"- events: **{total_events}**",
             f"- targeted entity/type/tag: **{report['target_checks_passed']}/{report['target_checks_total']}**",
             f"- gate가 제거한 unsupported alias: **{len(removed_unsupported_aliases)}**",
             f"- gate가 제거한 unsupported category tag: **{len(removed_unsupported_tags)}**",
             f"- 저장 후 unsupported alias: **{len(unsupported_persisted_aliases)}**",
             f"- 잘못된 source ID: **{len(invalid_sources)}**",
             f"- gate: **{gate_counts}**",
             f"- 비용: **${report['usage']['cost_usd']:.4f}**", "",
             "targeted 통과만으로 전체 ingestion 품질을 주장하지 않는다. category tag는 cited source에",
             "명시된 표현만 허용하고 canonical entity ID는 모델 출력이 아니라 backend hash로 생성한다."]
    (OUTPUT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    sessions = select_sessions()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "sessions.json").write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "freeze.json").write_text(json.dumps({
        "selection": "5 observed failure sessions + 7 SHA256 diversity sessions",
        "custom_ids": [f"{row['question_id']}::{row['session_id']}" for row in sessions],
        "prompt_sha256": digest(PROMPT), "schema_sha256": digest(SCHEMA),
        "claim_limit": "Targeted development calibration"}, indent=2) + "\n", encoding="utf-8")
    if args.run:
        key = getpass.getpass("OpenAI API key: ") if args.key_stdin else os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent; no requests sent")
        run(key, args.workers, sessions)
    print(json.dumps(audit(sessions), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
