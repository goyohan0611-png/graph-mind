"""Question-only diagnostic for the machine-executable Graph-MIND plan contract."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from longmemeval_adapter import load_instances
from longmemeval_ingestion_calibration import MODEL, RELATIONS, digest, response_text


DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
OUTPUT = Path("runs/graph-v2.1-executable-plan-v2.2")
QUESTION_IDS = (
    "gpt4_70e84552_abs", "gpt4_d31cdae3",  # compare
    "80ec1f4f_abs", "b6019101", "gpt4_7fce9456",  # count
    "60bf93ed_abs", "gpt4_4edbafa2", "07741c45",  # date/earliest/latest
    "09ba9854_abs", "720133ac",  # subtract/sum
)
EXPECTED_OPERATORS = {
    "gpt4_70e84552_abs": "COMPARE", "gpt4_d31cdae3": "COMPARE",
    "80ec1f4f_abs": "COUNT", "b6019101": "COUNT", "gpt4_7fce9456": "COUNT",
    "60bf93ed_abs": "DATE_DIFF", "gpt4_4edbafa2": "EARLIEST",
    "07741c45": "LATEST", "09ba9854_abs": "SUBTRACT", "720133ac": "SUM",
}
OPERATORS = ("LOOKUP", "COUNT", "SUM", "SUBTRACT", "DATE_DIFF", "COMPARE",
             "LATEST", "EARLIEST")
VALUE_FIELDS = ("OBJECT_TEXT", "NUMERIC_VALUE", "DATE_VALUE", "SESSION_DATE", "EVENT_COUNT")

PROMPT = """Compile one long-term-memory question into a machine-executable query plan.
The memory, answer, evidence labels, and official task type are unavailable. Do not answer.

Use only controlled ingestion relations. Put every value needed by the top-level operator in a
separate operand. Each operand is a typed filter over the complete memory index. Normalize explicit
calendar ranges to ISO start inclusive/end exclusive using question_date. A constraint relative to
another memory event, such as "before making an offer", must use time_relation and
time_reference_operand; never put symbolic text in an ISO field. Use null when the question does not
specify a value. relation_details, entity_terms, and category_tags are lexical constraints,
not guessed answers. For COUNT, choose a distinct key and set index_scope_required true. For
SUBTRACT, DATE_DIFF, and COMPARE emit operands in left/right order. For SUM, operands may describe
separate stored values or one repeated event class. Missing required operands must cause UNKNOWN.
The words current/latest/most recent require top-level LATEST; first/earliest require EARLIEST.
Questions about I/my/the narrator use USER_MEMORY. Use ASSISTANT_HISTORY only when the question
explicitly asks what the assistant previously said or recommended. For COUNT, aggregate_operand must
name exactly the operand being counted, and that operand needs a non-NONE distinct_by. If
empty_result is ZERO_IF_COMPLETE, no abstain_if rule may treat zero matches as missing evidence.
Return only the requested JSON."""

NULL_STRING = {"type": ["string", "null"]}
OPERAND = {"type": "object", "properties": {
    "name": {"type": "string"},
    "source_scope": {"type": "string", "enum": ["USER_MEMORY", "ASSISTANT_HISTORY", "BOTH"]},
    "relations": {"type": "array", "items": {"type": "string", "enum": list(RELATIONS)}},
    "relation_details": {"type": "array", "items": {"type": "string"}},
    "entity_terms": {"type": "array", "items": {"type": "string"}},
    "category_tags": {"type": "array", "items": {"type": "string"}},
    "value_field": {"type": "string", "enum": list(VALUE_FIELDS)},
    "value_type": {"type": "string", "enum": [
        "ENTITY", "TEXT", "NUMBER", "MONEY", "DURATION", "DATE", "BOOLEAN"]},
    "unit": NULL_STRING, "selector": {"type": "string", "enum": [
        "ALL", "UNIQUE", "LATEST", "EARLIEST"]},
    "time_start": NULL_STRING, "time_end_exclusive": NULL_STRING,
    "time_relation": {"type": "string", "enum": [
        "ANY", "BEFORE", "AFTER", "ON_OR_BEFORE", "ON_OR_AFTER"]},
    "time_reference_operand": NULL_STRING,
    "distinct_by": {"type": "string", "enum": [
        "NONE", "CANONICAL_ENTITY_ID", "OCCURRENCE_ID", "OBJECT_TEXT", "EVENT_ID"]},
    "required": {"type": "boolean"}},
    "required": ["name", "source_scope", "relations", "relation_details", "entity_terms",
        "category_tags", "value_field", "value_type", "unit", "selector", "time_start",
        "time_end_exclusive", "time_relation", "time_reference_operand", "distinct_by",
        "required"], "additionalProperties": False}
SCHEMA = {"type": "object", "properties": {
    "operator": {"type": "string", "enum": list(OPERATORS)},
    "operands": {"type": "array", "items": OPERAND},
    "aggregate_operand": NULL_STRING,
    "result_type": {"type": "string", "enum": [
        "ENTITY", "TEXT", "NUMBER", "MONEY", "DURATION", "DATE", "BOOLEAN"]},
    "result_unit": NULL_STRING,
    "empty_result": {"type": "string", "enum": ["ZERO_IF_COMPLETE", "EMPTY_LIST", "UNKNOWN"]},
    "index_scope_required": {"type": "boolean"},
    "abstain_if": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]}},
    "required": ["operator", "operands", "aggregate_operand", "result_type", "result_unit", "empty_result",
                 "index_scope_required", "abstain_if", "confidence"],
    "additionalProperties": False}


def questions() -> list[dict]:
    mapping = {row["question_id"]: row for row in load_instances(DATA)}
    return [{"question_id": question_id, "question": mapping[question_id]["question"],
             "question_date": mapping[question_id]["question_date"]}
            for question_id in QUESTION_IDS]


def payload(row: dict) -> dict:
    supplied = {"question": row["question"], "question_date": row["question_date"]}
    return {"model": MODEL, "store": False, "reasoning": {"effort": "low"},
            "input": [{"role": "system", "content": PROMPT},
                      {"role": "user", "content": json.dumps(supplied, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "executable_plan_v2",
                                 "strict": True, "schema": SCHEMA}},
            "max_output_tokens": 8192}


def call_api(key: str, body: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    request = Request("https://api.openai.com/v1/responses",
                      data=json.dumps(body).encode("utf-8"),
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        return json.load(response), time.perf_counter() - started


def load_responses() -> dict[str, dict]:
    result = {}
    path = OUTPUT / "responses.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                result[row["question_id"]] = row
    return result


def send(key: str, row: dict) -> dict:
    body = payload(row)
    record = {"question_id": row["question_id"], "request_sha256": digest(body),
              "timestamp": datetime.now(timezone.utc).isoformat()}
    started = time.perf_counter()
    try:
        raw, seconds = call_api(key, body)
        record.update(status="ok", plan=json.loads(response_text(raw)), usage=raw.get("usage", {}),
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


def run(key: str, workers: int) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "responses.jsonl"
    previous = load_responses()
    pending = [row for row in questions()
               if previous.get(row["question_id"], {}).get("status") != "ok"]
    with path.open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(send, key, row) for row in pending]
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                print(f"{record['question_id']} {record['status']}", flush=True)


def structural_audit(plan: dict, expected: str) -> list[str]:
    errors = []
    if plan["operator"] != expected:
        errors.append("OPERATOR_MISMATCH")
    if not plan["operands"]:
        errors.append("NO_OPERANDS")
    if expected in {"SUBTRACT", "DATE_DIFF", "COMPARE"} and len(plan["operands"]) != 2:
        errors.append("BINARY_OPERAND_COUNT")
    if expected == "COUNT":
        if not plan["index_scope_required"]:
            errors.append("COUNT_SCOPE_NOT_REQUIRED")
        operands_by_name = {operand["name"]: operand for operand in plan["operands"]}
        aggregate = operands_by_name.get(plan["aggregate_operand"])
        if aggregate is None or aggregate["distinct_by"] == "NONE":
            errors.append("COUNT_DISTINCT_KEY_MISSING")
        if (plan["empty_result"] == "ZERO_IF_COMPLETE"
                and any("NO_MATCH" in rule.upper() for rule in plan["abstain_if"])):
            errors.append("COUNT_EMPTY_CONTRADICTION")
    elif plan["aggregate_operand"] is not None:
        errors.append("UNEXPECTED_AGGREGATE_OPERAND")
    operand_names = {operand["name"] for operand in plan["operands"]}
    for operand in plan["operands"]:
        if operand["source_scope"] != "USER_MEMORY":
            errors.append(f"{operand['name']}:SOURCE_SCOPE_LEAK")
        if not operand["relations"]:
            errors.append(f"{operand['name']}:NO_RELATION")
        if operand["time_start"] and not re_iso(operand["time_start"]):
            errors.append(f"{operand['name']}:BAD_TIME_START")
        if operand["time_end_exclusive"] and not re_iso(operand["time_end_exclusive"]):
            errors.append(f"{operand['name']}:BAD_TIME_END")
        if operand["time_relation"] != "ANY" and not operand["time_reference_operand"]:
            errors.append(f"{operand['name']}:TIME_REFERENCE_MISSING")
        if (operand["time_reference_operand"] is not None
                and operand["time_reference_operand"] not in operand_names):
            errors.append(f"{operand['name']}:UNKNOWN_TIME_REFERENCE")
    return errors


def re_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def summarize() -> dict:
    responses = load_responses()
    audits = []
    for question_id in QUESTION_IDS:
        record = responses.get(question_id, {})
        errors = structural_audit(record["plan"], EXPECTED_OPERATORS[question_id]) \
            if record.get("status") == "ok" else [record.get("status", "MISSING")]
        audits.append({"question_id": question_id, "expected_operator": EXPECTED_OPERATORS[question_id],
                       "actual_operator": record.get("plan", {}).get("operator"),
                       "errors": errors, "passed": not errors})
    inp = sum(row.get("usage", {}).get("input_tokens", 0) for row in responses.values())
    out = sum(row.get("usage", {}).get("output_tokens", 0) for row in responses.values())
    report = {"role": "question-only executable-plan structural diagnostic",
              "questions": len(QUESTION_IDS),
              "api_success": sum(row.get("status") == "ok" for row in responses.values()),
              "structural_pass": sum(row["passed"] for row in audits), "audits": audits,
              "usage": {"input_tokens": inp, "output_tokens": out,
                        "cost_usd": inp / 1e6 * .25 + out / 1e6 * 2},
              "claim_limit": "No plan gold or execution result; structural executability only."}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "RESULTS.md").write_text(
        "# Executable plan v2 diagnostic\n\n"
        "> question과 question date만 사용한 구조 진단이다. plan 정확도나 executor 정답률이 아니다.\n\n"
        f"- API 성공: **{report['api_success']}/{len(QUESTION_IDS)}**\n"
        f"- structural pass: **{report['structural_pass']}/{len(QUESTION_IDS)}**\n"
        f"- 비용: **${report['usage']['cost_usd']:.4f}**\n",
        encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "questions.json").write_text(
        json.dumps(questions(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "freeze.json").write_text(json.dumps({
        "question_ids": list(QUESTION_IDS), "expected_operators": EXPECTED_OPERATORS,
        "prompt_sha256": digest(PROMPT), "schema_sha256": digest(SCHEMA),
        "model_input_excludes": ["memory", "answer", "evidence", "question_type"],
        "claim_limit": "Development structural diagnostic"}, indent=2) + "\n", encoding="utf-8")
    if args.run:
        key = getpass.getpass("OpenAI API key: ") if args.key_stdin else os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent; no requests sent")
        run(key, args.workers)
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
