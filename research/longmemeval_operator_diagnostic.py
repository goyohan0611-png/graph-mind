"""Question-only GPT diagnostic for LongMemEval execution operators."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from longmemeval_adapter import load_instances


DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
PILOT = Path("runs/graph-v2.1-longmemeval-audit/pilot-manifest.json")
OUTPUT = Path("runs/graph-v2.1-longmemeval-operator-diagnostic-v2")
MODEL = "gpt-5-mini"
OPERATORS = ("LOOKUP", "LATEST", "EARLIEST", "COUNT", "SUM", "SUBTRACT",
             "LIST", "COMPARE",
             "DATE_DIFF", "TEMPORAL_FILTER", "MULTI_FACT_SYNTHESIS",
             "ABSTAIN_CHECK")
PROMPT = """You design an execution plan for a long-term memory engine.
You receive only one question and its question date. You never see conversation history,
the reference answer, evidence labels, or official question type.

Identify the smallest set of operations the memory engine may need:
- LOOKUP: retrieve a fact or event
- LATEST: choose the newest applicable version
- EARLIEST: choose the first applicable event
- COUNT: count distinct facts/events
- SUM: add numeric durations or quantities
- SUBTRACT: compute a numeric difference or remaining amount
- LIST: return multiple items
- COMPARE: compare values or ordering
- DATE_DIFF: compute elapsed time
- TEMPORAL_FILTER: restrict facts by a date/range/relative time
- MULTI_FACT_SYNTHESIS: combine facts from multiple events
- ABSTAIN_CHECK: verify evidence is sufficient and return UNKNOWN if it is not

Do not answer the question. Relation hints must be short UPPER_SNAKE_CASE predicates.
Return only the requested JSON."""

SCHEMA = {
    "type": "object",
    "properties": {
        "primary_operator": {"type": "string", "enum": list(OPERATORS)},
        "operators": {"type": "array", "items": {"type": "string", "enum": list(OPERATORS)}},
        "answer_shape": {"type": "string", "enum": [
            "ENTITY", "DATE", "DURATION", "NUMBER", "BOOLEAN", "LIST", "TEXT", "UNKNOWN"]},
        "entities": {"type": "array", "items": {"type": "string"}},
        "relation_hints": {"type": "array", "items": {"type": "string"}},
        "time_constraints": {"type": "array", "items": {"type": "string"}},
        "requires_aggregation": {"type": "boolean"},
        "asks_about_assistant": {
            "type": "boolean",
            "description": "true when the question asks what the ASSISTANT itself said, "
                           "recommended or did in an earlier conversation, rather than a fact "
                           "about the user",
        },
        "missing_evidence_policy": {"type": "string", "enum": ["UNKNOWN", "EMPTY"]},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
    },
    "required": ["primary_operator", "operators", "answer_shape", "entities",
                 "relation_hints", "time_constraints", "requires_aggregation",
                 "asks_about_assistant", "missing_evidence_policy", "confidence"],
    "additionalProperties": False,
}


def digest(value) -> str:
    encoded = (value if isinstance(value, str)
               else json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def response_text(response: dict) -> str:
    pieces = [piece for item in response.get("output", [])
              if item.get("type") == "message" for piece in item.get("content", [])]
    if any(piece.get("type") == "refusal" for piece in pieces):
        raise ValueError("Model refusal")
    return "".join(piece["text"] for piece in pieces
                   if piece.get("type") == "output_text")


def payload(question: dict) -> dict:
    return {"model": MODEL, "store": False,
            "input": [{"role": "system", "content": PROMPT},
                      {"role": "user", "content":
                       f"QUESTION_DATE: {question['question_date']}\nQUESTION: {question['question']}"}],
            "reasoning": {"effort": "low"},
            "text": {"format": {"type": "json_schema", "name": "memory_execution_plan",
                                  "strict": True, "schema": SCHEMA}},
            "max_output_tokens": 2048}


def prepare() -> list[dict]:
    manifest = json.loads(PILOT.read_text(encoding="utf-8"))
    selected = set(manifest["question_ids"])
    rows = [row for row in load_instances(DATA) if row["question_id"] in selected]
    public = [{"question_id": row["question_id"], "question": row["question"],
               "question_date": row["question_date"]} for row in rows]
    public.sort(key=lambda row: row["question_id"])
    OUTPUT.mkdir(parents=True, exist_ok=True)
    frozen = {"role": "question-only development operator diagnostic",
              "model": MODEL, "questions": len(public),
              "request_options": {"reasoning_effort": "low", "max_output_tokens": 2048},
              "question_sha256": digest(public), "prompt_sha256": digest(PROMPT),
              "schema_sha256": digest(SCHEMA),
              "model_input_excludes": ["history", "answer", "answer_session_ids",
                                       "has_answer", "official_question_type"]}
    (OUTPUT / "questions.json").write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "freeze.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    return public


def read_records() -> dict[str, dict]:
    path = OUTPUT / "responses.jsonl"
    if not path.exists():
        return {}
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            records[row["question_id"]] = row
    return records


def run(key: str, workers: int, limit: int | None) -> None:
    questions = prepare()
    freeze = json.loads((OUTPUT / "freeze.json").read_text(encoding="utf-8"))
    if (freeze["question_sha256"] != digest(questions)
            or freeze["prompt_sha256"] != digest(PROMPT)
            or freeze["schema_sha256"] != digest(SCHEMA)):
        raise ValueError("Frozen diagnostic inputs changed")
    previous = read_records()
    pending = [row for row in questions if row["question_id"] not in previous]
    if limit is not None:
        pending = pending[:limit]

    def send(question: dict) -> dict:
        body = payload(question)
        record = {"question_id": question["question_id"],
                  "request_sha256": digest(body),
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        started = time.perf_counter()
        try:
            request = Request("https://api.openai.com/v1/responses",
                              data=json.dumps(body).encode("utf-8"),
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"})
            with urlopen(request, timeout=90) as response:
                raw = json.load(response)
            text = response_text(raw)
            plan = json.loads(text)
            record.update(status="ok", plan=plan, usage=raw.get("usage", {}),
                          response_id=raw.get("id"), model=raw.get("model"))
        except HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read()).get("error", {}).get("code", "")
            except (ValueError, AttributeError):
                pass
            record.update(status="transport_error", error="HTTPError",
                          http_status=error.code, api_error_code=detail)
        except (URLError, TimeoutError) as error:
            record.update(status="transport_error", error=type(error).__name__)
        except (ValueError, KeyError, TypeError) as error:
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

    with (OUTPUT / "responses.jsonl").open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(send, row): row for row in pending}
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                print(f"{record['question_id']}: {record['status']}", flush=True)
    summarize()


def summarize() -> dict:
    questions = prepare()
    records = read_records()
    complete = [records[q["question_id"]] for q in questions
                if q["question_id"] in records and records[q["question_id"]]["status"] == "ok"]
    primary = Counter(row["plan"]["primary_operator"] for row in complete)
    all_operators = Counter(op for row in complete for op in set(row["plan"]["operators"]))
    shapes = Counter(row["plan"]["answer_shape"] for row in complete)
    billed = [row for row in records.values() if row.get("usage")]
    input_tokens = sum(row.get("usage", {}).get("input_tokens", 0) for row in billed)
    output_tokens = sum(row.get("usage", {}).get("output_tokens", 0) for row in billed)
    report = {"role": "question-only operator diagnostic", "model": MODEL,
              "questions": len(questions), "successful": len(complete),
              "failed_or_missing": len(questions) - len(complete),
              "primary_operators": dict(sorted(primary.items())),
              "all_operators": dict(sorted(all_operators.items())),
              "answer_shapes": dict(sorted(shapes.items())),
              "requires_aggregation": sum(row["plan"]["requires_aggregation"] for row in complete),
              "confidence": dict(sorted(Counter(row["plan"]["confidence"] for row in complete).items())),
              "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                        "standard_cost_usd": input_tokens / 1e6 * .25 + output_tokens / 1e6 * 2.0},
              "claim_limit": "No operator gold labels; this is schema design evidence, not accuracy."}
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# LongMemEval 60문항 operator diagnostic", "",
             "> GPT에는 question과 question date만 제공했다. history·정답·evidence·공식 type은 제공하지 않았다.",
             "> operator gold label이 없으므로 정확도 결과가 아니라 executor 범위 설계용 진단이다.", "",
             f"- 성공: **{len(complete)}/{len(questions)}**",
             f"- 실제 사용: input **{input_tokens:,}**, output **{output_tokens:,} tokens**, 추정 **${report['usage']['standard_cost_usd']:.4f}**",
             f"- aggregation 필요 판정: **{report['requires_aggregation']}문항**", "",
             "## Primary operator", "", "| operator | 문항 |", "|---|---:|"]
    for operator, count in primary.most_common():
        lines.append(f"| {operator} | {count} |")
    lines += ["", "## 전체 operator 출현", "", "| operator | 문항 |", "|---|---:|"]
    for operator, count in all_operators.most_common():
        lines.append(f"| {operator} | {count} |")
    lines += ["", "## 판정", "",
              "- LOOKUP/LATEST만으로 충분한지, COUNT·SUM·DATE_DIFF·TEMPORAL_FILTER가 필요한지 이 분포로 결정한다.",
              "- LOW confidence와 복합 operator 문항을 먼저 수동 검토한 뒤 ingestion relation ontology를 고정한다.",
              "- 같은 60문항은 prompt/schema 변경에 사용하면 development set으로만 유지한다."]
    (OUTPUT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    prepare()
    if args.run:
        key = (getpass.getpass("OpenAI API key: ") if args.key_stdin
               else os.environ.get("OPENAI_API_KEY", ""))
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent; no requests sent")
        run(key, args.workers, args.limit)
    else:
        summarize()


if __name__ == "__main__":
    main()
