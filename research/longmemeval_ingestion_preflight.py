"""Token and cost preflight for the frozen LongMemEval development pilot."""
from __future__ import annotations

import json
from pathlib import Path
import statistics

import tiktoken

from longmemeval_adapter import load_instances


DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
PILOT = Path("runs/graph-v2.1-longmemeval-audit/pilot-manifest.json")
OUTPUT = Path("runs/graph-v2.1-longmemeval-ingestion-preflight")
MODEL = "gpt-5-mini"
PRICE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-5-mini"
BATCH_SOURCE = "https://developers.openai.com/api/reference/resources/batches"
STANDARD_INPUT_PER_M = 0.25
STANDARD_OUTPUT_PER_M = 2.00
BATCH_INPUT_PER_M = STANDARD_INPUT_PER_M / 2
BATCH_OUTPUT_PER_M = STANDARD_OUTPUT_PER_M / 2

INGESTION_PROMPT = """Extract durable long-term memory events from exactly one chat session.
The later question is not available. Do not optimize for or guess a future question.
Keep explicit user facts, preferences, plans, completed activities, state changes, dates,
quantities, relationships, and assistant commitments. Omit generic knowledge, puzzles,
boilerplate, and transient small talk. Every event must cite one or more turn IDs.
Return only the requested structured JSON."""

PLAN_PROMPT = """Convert one long-term-memory question into a typed execution plan.
Classify the required operators from LOOKUP, LATEST, COUNT, SUM, LIST, COMPARE,
DATE_DIFF, TEMPORAL_FILTER, MULTI_FACT_SYNTHESIS, and ABSTAIN_CHECK. Identify entities,
relations, time constraints, and whether missing evidence must return UNKNOWN.
Do not answer the question. Return only structured JSON."""

ANSWER_PROMPT = """Answer the question using only the supplied evidence sessions.
If the evidence does not establish the answer, explicitly say the information was not mentioned.
Return a concise answer."""


def serialize_session(date: str, session_id: str, turns: list[dict]) -> str:
    lines = [f"SESSION_ID: {session_id}", f"SESSION_DATE: {date}"]
    for index, turn in enumerate(turns):
        lines.append(f"TURN_{index:03d} [{turn['role']}]: {turn['content']}")
    return "\n".join(lines)


def tokens(encoding, *parts: str) -> int:
    return sum(len(encoding.encode(part)) for part in parts) + 12


def cost(input_tokens: int, output_tokens: int, batch: bool) -> float:
    input_rate = BATCH_INPUT_PER_M if batch else STANDARD_INPUT_PER_M
    output_rate = BATCH_OUTPUT_PER_M if batch else STANDARD_OUTPUT_PER_M
    return input_tokens / 1_000_000 * input_rate + output_tokens / 1_000_000 * output_rate


def main() -> None:
    pilot = json.loads(PILOT.read_text(encoding="utf-8"))
    selected_ids = set(pilot["question_ids"])
    rows = [row for row in load_instances(DATA) if row["question_id"] in selected_ids]
    encoding = tiktoken.encoding_for_model(MODEL)

    ingestion_inputs = []
    session_bytes = []
    plan_inputs = []
    answer_inputs = []
    for row in rows:
        serialized = []
        for date, session_id, turns in zip(row["haystack_dates"],
                                           row["haystack_session_ids"],
                                           row["haystack_sessions"]):
            content = serialize_session(date, session_id, turns)
            serialized.append((session_id, content))
            ingestion_inputs.append(tokens(encoding, INGESTION_PROMPT, content))
            session_bytes.append(len(content.encode("utf-8")))
        plan_inputs.append(tokens(encoding, PLAN_PROMPT, row["question"],
                                  row["question_date"]))
        evidence_ids = set(row["answer_session_ids"])
        evidence_text = "\n\n".join(content for session_id, content in serialized
                                      if session_id in evidence_ids)
        answer_inputs.append(tokens(encoding, ANSWER_PROMPT, row["question"],
                                    row["question_date"], evidence_text))

    ingestion_total = sum(ingestion_inputs)
    plan_total = sum(plan_inputs)
    answer_total = sum(answer_inputs)
    scenarios = []
    for ingestion_output_per_request in (128, 256, 512, 1024):
        ingestion_output = len(ingestion_inputs) * ingestion_output_per_request
        plan_output = len(rows) * 200
        answer_output = len(rows) * 100
        total_input = ingestion_total + plan_total + answer_total
        total_output = ingestion_output + plan_output + answer_output
        scenarios.append({
            "ingestion_output_tokens_per_session": ingestion_output_per_request,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "standard_cost_usd": cost(total_input, total_output, False),
            "batch_cost_usd": cost(total_input, total_output, True),
        })

    calibration_count = 12
    calibration_input = sum(sorted(ingestion_inputs)[:calibration_count])
    calibration_output = calibration_count * 512
    report = {
        "role": "LongMemEval 60-question ingestion cost preflight",
        "api_calls": 0,
        "model": MODEL,
        "tokenizer": encoding.name,
        "pricing_checked": "2026-09-11",
        "pricing": {
            "standard_input_per_million": STANDARD_INPUT_PER_M,
            "standard_output_per_million": STANDARD_OUTPUT_PER_M,
            "batch_input_per_million": BATCH_INPUT_PER_M,
            "batch_output_per_million": BATCH_OUTPUT_PER_M,
            "model_price_source": PRICE_SOURCE,
            "batch_discount_source": BATCH_SOURCE,
        },
        "pilot": {
            "questions": len(rows),
            "independent_session_requests": len(ingestion_inputs),
            "session_input_tokens": ingestion_total,
            "session_input_bytes": sum(session_bytes),
            "session_tokens_min": min(ingestion_inputs),
            "session_tokens_median": statistics.median(ingestion_inputs),
            "session_tokens_p95": sorted(ingestion_inputs)[int(len(ingestion_inputs) * .95)],
            "session_tokens_max": max(ingestion_inputs),
            "question_plan_requests": len(rows),
            "question_plan_input_tokens": plan_total,
            "oracle_evidence_answer_requests": len(rows),
            "oracle_evidence_answer_input_tokens": answer_total,
        },
        "scenarios": scenarios,
        "calibration": {
            "session_requests": calibration_count,
            "selection": "12 shortest sessions for transport/schema smoke test",
            "input_tokens": calibration_input,
            "assumed_output_tokens": calibration_output,
            "standard_cost_ceiling_usd": cost(calibration_input, calibration_output, False),
        },
        "batch_constraints": {
            "requests_limit": 50_000,
            "file_size_limit_mb": 200,
            "tier_1_queued_input_token_limit_from_model_page": 5_000_000,
            "requires_split_at_tier_1": ingestion_total > 5_000_000,
        },
        "claim_limits": [
            "Token counts are local tokenizer counts, not billed usage.",
            "Output tokens are scenarios until calibration is run.",
            "Histories are independent benchmark instances and are not deduplicated across questions.",
            "Oracle evidence is used only to estimate final-answer context size.",
        ],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# LongMemEval ingestion 비용 사전평가", "",
        "> 동결한 60문항의 모든 session을 질문과 무관하게 독립 ingestion하는 조건이다.",
        "> API 호출은 없으며 token은 local tokenizer 계산값이다.", "",
        "## 입력량", "",
        f"- 독립 session 요청: **{len(ingestion_inputs):,}개**",
        f"- ingestion 입력: **{ingestion_total:,} tokens**",
        f"- session당 입력 중앙값/p95/최대: **{statistics.median(ingestion_inputs):,.0f} / {sorted(ingestion_inputs)[int(len(ingestion_inputs) * .95)]:,} / {max(ingestion_inputs):,} tokens**",
        f"- 질문 plan 60건 입력: **{plan_total:,} tokens**",
        f"- oracle evidence 기반 최종 답변 60건 입력 추정: **{answer_total:,} tokens**", "",
        "## 출력량별 전체 비용", "",
        "GPT-5 mini의 공식 standard 가격과 Batch 50% 할인을 적용했다.", "",
        "| session당 ingestion 출력 | 전체 입력 | 전체 출력 | standard | Batch |",
        "|---:|---:|---:|---:|---:|",
    ]
    for row in scenarios:
        lines.append(f"| {row['ingestion_output_tokens_per_session']} | {row['input_tokens']:,} | {row['output_tokens']:,} | ${row['standard_cost_usd']:.3f} | ${row['batch_cost_usd']:.3f} |")
    lines += ["", "## 실행 판정", "",
              f"- 12-session calibration 최대 추정 비용: **${report['calibration']['standard_cost_ceiling_usd']:.4f}**",
              f"- Tier 1 Batch queue 5M token 가정에서 분할 필요: **{report['batch_constraints']['requires_split_at_tier_1']}**",
              "- 전체 batch 전에 12개 session으로 JSON schema 유효성, event 수와 실제 output token을 측정한다.",
              "- session ID가 겹쳐도 instance별 내용이 달라 benchmark history 사이에서 ingestion을 공유하지 않는다.",
              "- 질문 operator 진단을 먼저 실행해 relation ontology와 COUNT/SUM/LATEST/DATE_DIFF executor 범위를 고정한다."]
    (OUTPUT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sessions={len(ingestion_inputs)} ingestion_tokens={ingestion_total}")
    print(f"calibration_ceiling=${report['calibration']['standard_cost_ceiling_usd']:.4f}")


if __name__ == "__main__":
    main()
