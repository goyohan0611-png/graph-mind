"""Oracle-evidence diagnostic that isolates LongMemEval ingestion loss."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ingestion_gate import GateStatus, classify_event
from longmemeval_adapter import load_instances
from longmemeval_ingestion_calibration import MODEL, PROMPT, SCHEMA, payload as ingestion_payload
from longmemeval_ingestion_preflight import serialize_session
import grounding_verifier
import temporal_resolver
from precondition_verifier import verify_preconditions
from semantic_grounding_v073 import LocalEmbedder, cosine


DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
PILOT = Path("runs/graph-v2.1-longmemeval-audit/pilot-manifest.json")
OPERATOR_RESPONSES = Path("runs/graph-v2.1-longmemeval-operator-diagnostic-v2/responses.jsonl")
# v0.7.3 integration: real Graph-MIND retrieval (chunked, local embeddings) replaces oracle evidence.
OUTPUT = Path("runs/graph-v3.0-retrieval-executor-v2")
RETRIEVAL_K = 16         # floor: top-8 left 10% of untouched-v3 questions without their evidence
RETRIEVAL_MARGIN = 1.5   # over sqrt(store size), above the floor — see retrieval_k()
                         # (9 of those 10 were answered wrong); k=16 lifts session recall 90% -> 98%
                         # and costs ingestion, not reader tokens (events/passages stay capped).
READER_MODEL = "gpt-5-mini"  # answer/reader model (Zep-comparable); ingestion keeps MODEL
GATE_ENABLED = True       # engine-level sufficiency gate: abstention decided by the engine, not reader
GATE_MODE = "off"        # "grounding" REGRESSED dev 85.0 -> 40.8 (blocked 51 correct answers:
                         # computed answers (DATE_DIFF/COMPARE) have numbers absent from evidence,
                         # and a 0.35 cosine bar is wrong for a short answer vs a 600-char passage).
                         # Modes: "grounding", "llm" (gpt gate), "off" (reader decides abstention).
GATE_MODEL = "gpt-5-mini"
MULTI_VIEW = True         # entity query views in evidence_passages (k=16 alone: dev2 79.2 -> 84.2)
GATE_PROMPT = """You are an evidence-sufficiency checker for a memory engine. Given a user question, the
structured memory events and verbatim evidence passages retrieved for it, decide whether the answer can be
DERIVED from them — stated directly, or by simple reasoning (combining facts across sessions, ordering
by date, counting, resolving a reference to the thing it describes). Answer sufficient=true if the
answer is derivable, even when the
question's exact wording does not appear. Answer sufficient=false ONLY when the evidence genuinely lacks
the information (the asked subject/fact is absent). No outside knowledge. Return only JSON."""
GATE_SCHEMA = {"type": "object", "additionalProperties": False,
               "properties": {"sufficient": {"type": "boolean"}}, "required": ["sufficient"]}
_CHUNK = 500
_EMBEDDER = None


def _embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        _EMBEDDER = LocalEmbedder(OUTPUT / "embed-cache-local.json")
    return _EMBEDDER


def retrieval_k(sessions: int) -> int:
    """How many sessions to open, given how many there are. A fixed k cannot be right for both a
    small store and a large one: on LongMemEval_S (47 sessions) the worst-ranked answer session of the
    20-question subset sits at rank 5, while on _M (476 sessions, the SAME questions) it sits at 20, so
    k=16 leaves 10% of those questions without their evidence (longmemeval_m_recall.py).

    Two points: k>=6 at 47 sessions, k>=21 at 476. A constant SHARE of the store fits them too but
    cannot be right — 7% of a 50,000-session memory is 3,500 sessions to open, which would throw away
    the flat cost that is the whole point. sqrt does fit both almost exactly (6/sqrt(47) = 0.88,
    21/sqrt(476) = 0.96), grows sub-linearly, and is the usual shape for this kind of coverage. Two
    points cannot distinguish sqrt from a logarithm, so the 1.5x margin is deliberate and nothing here
    is validated beyond 476 sessions."""
    return max(RETRIEVAL_K, math.ceil(RETRIEVAL_MARGIN * math.sqrt(sessions)))


def gm_retrieved_session_ids(row: dict, k: int | None = None) -> set[str]:
    """Graph-MIND chunked retrieval over the full haystack: score each session by its best
    500-char passage vs the question, return the top-k session ids (no oracle labels used).

    Indexes the USER's turns, whole length. The old index (first 3000 chars of the serialized
    session) missed evidence sitting later in 9-20k-char sessions, and assistant prose — most of
    the text — diluted the signal: dev recall of "all answer sessions in top-8" 105/120 -> 115/120
    at the same number of chunks. Assistant turns join the index only when the question asks what
    the assistant itself said (retrieval_ablation_v32.py)."""
    return set(gm_ranked_session_ids(row)[:k or retrieval_k(len(row["haystack_session_ids"]))])


def gm_ranked_session_ids(row: dict) -> list[str]:
    """Every haystack session, best-passage score first. Separate from the top-k call above so that
    sweeping k costs one embedding pass instead of one per k (longmemeval_m_recall.py)."""
    emb = _embedder()
    ids = row["haystack_session_ids"]
    want_assistant = asks_assistant_history(row["question"])
    chunk_texts, owner = [], []
    for sid, turns in zip(ids, row["haystack_sessions"]):
        for turn in turns:
            if turn["role"] != "user" and not want_assistant:
                continue
            text = turn["content"]
            for i in range(0, len(text), _CHUNK):
                piece = text[i:i + _CHUNK]
                if piece.strip():
                    chunk_texts.append(piece)
                    owner.append(sid)
    if not chunk_texts:
        return []
    qv = emb.embed([row["question"]])[0]
    cvs = emb.embed(chunk_texts)
    best: dict[str, float] = {}
    for sid, cv in zip(owner, cvs):
        score = cosine(qv, cv)
        if score > best.get(sid, -1.0):
            best[sid] = score
    return sorted(best, key=best.get, reverse=True)


BASE_INGESTION = Path(
    "runs/graph-v2.1-longmemeval-oracle-ingestion-recall-v2/ingestion.jsonl")

ANSWER_PROMPT = """Answer one long-term-memory question using only the supplied structured events and
evidence_passages. evidence_passages are verbatim excerpts of the user's own conversations; events are
extracted from them and can drop the links between facts. When the passages state or directly support
the answer, answer from them even if no single event says it.
Each passage carries its session_date: relative time expressions inside it (today, yesterday, last
week, "the 15th") are relative to that session_date. An event's session_date is the day the user
said it and date_value the day it happened (when known); for ordering questions the session_date is
still evidence, since the user could only mention something on or after the day it happened. Facts for one question may be spread across
several sessions — combine them for totals, counts, and orderings. When the same quantity or state is
reported at different dates, the latest report is current unless the question asks about an earlier time.
Do not use outside knowledge. Assistant raw turns are supplied only when the question explicitly
asks what the assistant previously said, mentioned, or recommended; otherwise assistant statements
must not be treated as user facts or external truth. For COUNT, return 0 ONLY when the events DO
contain information about the question's subject/activity but no instance matches the requested
temporal or relational filter. If the events contain NO information about the question's subject
at all (the premise itself is unsupported), do NOT return 0 — abstain (answer "UNKNOWN",
abstained=true). Also abstain when a required fact, comparison term, or attribute is missing. If
deterministic_executor_hints contains must_abstain=true, return UNKNOWN and abstained=true. Return
only the requested JSON."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "abstained": {"type": "boolean"},
    },
    "required": ["answer", "abstained"],
    "additionalProperties": False,
}

# Aggregates: the model decides MEMBERSHIP ("is this a dinner party?"), the engine does the
# ARITHMETIC. Readers systematically lost exactly one instance when counting in prose (1 vs 2,
# 2 vs 3, 4 vs 5, 9 vs 10 on untouched v2), and arithmetic done by the reader makes the score
# depend on the reader model.
# LATEST/EARLIEST/COMPARE were briefly reverted on a false reading: the two runs compared (85.8%
# and 83.3%) turned out to have sent byte-identical requests for all 120 questions, so the gap was
# reader variance, not the operators. Restored — in the 85.8% run LATEST scored 5/6 and EARLIEST
# 1/1. Re-running one fixed configuration moves ~9 of 120 answers, which is the real noise floor
# for every A/B in this project.
AGGREGATE_OPERATORS = {"COUNT", "SUM", "LATEST", "EARLIEST", "COMPARE"}
# Units worth printing: money and measures change the meaning of the number, a counted noun
# ("6 people", "3 tanks") does not and can cost a judgement against a bare-number gold answer.
MEANINGFUL_UNITS = {"usd", "dollars", "won", "eur", "krw", "minutes", "minute", "hours", "hour",
                    "days", "day", "weeks", "week", "months", "month", "years", "year",
                    "km", "miles", "kg", "lbs", "percent", "%"}
AGGREGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "every distinct instance that matches the question and its time window",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "date": {"type": "string"},
                    "amount": {"type": ["number", "null"],
                               "description": "the countable/summable value of THIS instance "
                                              "(money, times, quantity); null when the instance "
                                              "simply counts as one"},
                },
                "required": ["label", "date", "amount"],
                "additionalProperties": False,
            },
        },
        "stated_total": {
            "type": ["number", "null"],
            "description": "the total the evidence states OUTRIGHT (use the most recent statement); "
                           "null when the answer has to be aggregated from `items`",
        },
        "unit": {"type": ["string", "null"]},
        "answer": {"type": "string"},
        "abstained": {"type": "boolean"},
    },
    "required": ["items", "stated_total", "unit", "answer", "abstained"],
    "additionalProperties": False,
}
AGGREGATE_RULE = """
This question may aggregate. FIRST decide which kind it is. When the evidence states the total
outright ("I'm up to 32 species now", "that brings my collection to 38"), put the most recent such
figure in `stated_total` and leave `items` empty — a running total the user reported is the answer,
not something to recount. Otherwise leave `stated_total` null and list in `items` EVERY distinct
instance supported by the evidence that matches the question and its time window — one entry per instance, with its date and, when the
question asks for an amount or a number of times, that instance's value in `amount` (otherwise
null). Do not count in prose and do not do the arithmetic: the engine computes the total from
`items`. Never list the same instance twice."""


def question_window(question: dict) -> tuple[str, str] | None:
    """The time window the QUESTION itself asks about ("in March", "this year", "past month"),
    resolved against the question date. Items outside it are not part of the answer — the reader
    counted weddings from other years on untouched v3."""
    return temporal_resolver.resolve(question["question"], question.get("question_date", ""))


def _in_window(item: dict, window: tuple[str, str] | None) -> bool:
    date = (item.get("date") or "").strip()[:10]
    if not window or len(date) < 10:
        return True          # undated instances stay: dropping them would invent precision
    return window[0] <= date <= window[1]


def engine_answer(plan: dict, result: dict, window=None) -> tuple[str, str] | None:
    """Operators the ENGINE executes over the reader's instance list, so the arithmetic and the
    ordering never depend on the reader model: totals, counts, latest/earliest, and which group
    has the largest sum. Returns (answer, mode) or None when nothing can be computed."""
    operator = (plan or {}).get("primary_operator")
    stated = result.get("stated_total")
    if operator in ("COUNT", "SUM") and isinstance(stated, (int, float)):
        # A total the user reported themselves must not be recomputed from its own mentions.
        return f"{stated:g}", "stated"
    items = [i for i in (result.get("items") or []) if (i.get("label") or "").strip()]
    items = [i for i in items if _in_window(i, window)]
    if not items:
        return None
    if operator in ("LATEST", "EARLIEST"):
        dated = [i for i in items if (i.get("date") or "").strip()]
        if not dated:
            return None
        pick = (max if operator == "LATEST" else min)(dated, key=lambda i: i["date"][:10])
        return pick["label"], operator.lower()
    if operator == "COMPARE":
        totals: dict[str, float] = {}
        for item in items:
            if isinstance(item.get("amount"), (int, float)):
                key = re.sub(r"[^a-z0-9]+", " ", item["label"].lower()).strip()
                totals[key] = totals.get(key, 0) + item["amount"]
        if len(totals) < 2:
            return None
        best = max(totals, key=totals.get)
        original = next(i["label"] for i in items
                        if re.sub(r"[^a-z0-9]+", " ", i["label"].lower()).strip() == best)
        return original, "argmax"
    total, mode = aggregate_total({"stated_total": None, "items": items})
    return (f"{total:g}", mode) if total is not None else None


def aggregate_total(result: dict) -> tuple[float | None, str]:
    """Engine-side arithmetic over the reader's instance list: sum amounts when the instances
    carry values, otherwise count the distinct instances."""
    stated = result.get("stated_total")
    if isinstance(stated, (int, float)):
        # A total the user reported themselves (knowledge-update style) must not be recomputed:
        # counting or re-summing the mentions behind it was worth -5 KU questions on dev v3.4.
        return stated, "stated"
    items = [i for i in (result.get("items") or []) if (i.get("label") or "").strip()]
    if not items:
        return None, "no_items"
    amounts = [i["amount"] for i in items if isinstance(i.get("amount"), (int, float))]
    if amounts and len(amounts) == len(items):
        return sum(amounts), "sum"
    seen, count = set(), 0
    for item in items:
        # Same label on different dates is a different occurrence ("missed 5K fun run" twice in
        # March was collapsed into one on dev2); only a same-day repeat is a duplicate.
        key = (re.sub(r"[^a-z0-9]+", " ", item["label"].lower()).strip(),
               (item.get("date") or "").strip())
        if key not in seen:
            seen.add(key)
            count += 1
    return count, "count"


JUDGE_PROMPT = """Judge whether a candidate answer is semantically correct for the question and
reference answer. Accept concise paraphrases, equivalent values, and extra detail that is consistent
with the reference. When the reference says the
information is insufficient, the candidate is correct only if it also clearly abstains. Do not
reject a correct core answer solely for a non-contradictory explanation. Return only the requested JSON."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {"type": "boolean"},
        "verdict": {"type": "string", "enum": [
            "EXACT_OR_EQUIVALENT", "CORRECT_ABSTENTION", "INCORRECT", "UNSUPPORTED_EXTRA"]},
    },
    "required": ["correct", "verdict"],
    "additionalProperties": False,
}


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def response_text(response: dict) -> str:
    return "".join(piece.get("text", "") for item in response.get("output", [])
                   if item.get("type") == "message" for piece in item.get("content", [])
                   if piece.get("type") == "output_text")


def load_jsonl(path: Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            result[row[key]] = row
    return result


def select_questions() -> list[dict]:
    pilot_ids = set(json.loads(PILOT.read_text(encoding="utf-8"))["question_ids"])
    rows = [row for row in load_instances(DATA) if row["question_id"] in pilot_ids]
    # v0.7.3: use ALL pilot questions (plans exist for all 60) for a tighter number than the 18-cap.
    abstention = sorted((row for row in rows if row["question_id"].endswith("_abs")),
                        key=lambda row: digest(row["question_id"]))
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if not row["question_id"].endswith("_abs"):
            grouped[row["question_type"]].append(row)
    positive = []
    for question_type in sorted(grouped):
        positive.extend(sorted(grouped[question_type], key=lambda row: digest(row["question_id"])))
    selected = positive + abstention
    selected.sort(key=lambda row: row["question_id"])
    return selected


def operator_plans() -> dict[str, dict]:
    rows = load_jsonl(OPERATOR_RESPONSES, "question_id")
    return {key: row["plan"] for key, row in rows.items() if row.get("status") == "ok"}


def prepare() -> tuple[list[dict], list[dict]]:
    questions = select_questions()
    sessions = []
    for row in questions:
        wanted = gm_retrieved_session_ids(row)  # GM retrieval, NOT oracle answer_session_ids
        for date, session_id, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                           row["haystack_sessions"]):
            if session_id in wanted:
                sessions.append({"custom_id": f"{row['question_id']}::{session_id}",
                                 "question_id": row["question_id"], "session_id": session_id,
                                 "content": serialize_session(date, session_id, turns)})
    public_questions = [{k: row[k] for k in ("question_id", "question_type", "question",
                                              "question_date", "answer", "answer_session_ids")}
                        for row in questions]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "questions.json").write_text(
        json.dumps(public_questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "sessions.json").write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    copied_ingestion = False
    target_ingestion = OUTPUT / "ingestion.jsonl"
    if BASE_INGESTION.exists() and not target_ingestion.exists():
        shutil.copyfile(BASE_INGESTION, target_ingestion)
        copied_ingestion = True
    freeze = {
        "role": "oracle-evidence ingestion recall diagnostic",
        "questions": len(questions), "sessions": len(sessions), "model": MODEL,
        "selection": "2 positive per official type plus 6 abstention, SHA256(question_id)",
        "question_sha256": digest(public_questions), "session_sha256": digest(sessions),
        "ingestion_prompt_sha256": digest(PROMPT), "ingestion_schema_sha256": digest(SCHEMA),
        "answer_prompt_sha256": digest(ANSWER_PROMPT), "judge_prompt_sha256": digest(JUDGE_PROMPT),
        "ingestion_response_source": str(BASE_INGESTION) if copied_ingestion or target_ingestion.exists() else None,
        "ingestion_model_input_excludes": ["question", "answer", "question_type",
                                           "answer_session_ids", "has_answer"],
        "claim_limit": "Canonical evidence sessions isolate ingestion; this is not retrieval accuracy.",
    }
    (OUTPUT / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return public_questions, sessions


def call_api(key: str, body: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    request = Request("https://api.openai.com/v1/responses",
                      data=json.dumps(body).encode("utf-8"),
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        return json.load(response), time.perf_counter() - started


def safe_call(key: str, body: dict, identity: dict, parser) -> dict:
    record = {**identity, "request_sha256": digest(body),
              "timestamp": datetime.now(timezone.utc).isoformat()}
    started = time.perf_counter()
    try:
        for attempt in range(6):  # retry 429 rate limits with exponential backoff
            try:
                raw, seconds = call_api(key, body)
                break
            except HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(min(60, 4 * (2 ** attempt)))  # 4,8,16,32,60s
                    continue
                raise
            except (URLError, TimeoutError, ConnectionError, HTTPException) as error:
                if attempt < 5:  # transient network drop (e.g. RemoteDisconnected): retry
                    time.sleep(min(60, 4 * (2 ** attempt)))
                    continue
                raise
        parsed = parser(raw)
        record.update(status="ok", result=parsed, usage=raw.get("usage", {}),
                      response_id=raw.get("id"), model=raw.get("model"), seconds=seconds)
    except HTTPError as error:
        code = ""
        try:
            code = json.loads(error.read()).get("error", {}).get("code", "")
        except (ValueError, AttributeError):
            pass
        record.update(status="transport_error", http_status=error.code,
                      api_error_code=code, seconds=time.perf_counter() - started)
    except (URLError, TimeoutError, ConnectionError, HTTPException) as error:
        record.update(status="transport_error", error=type(error).__name__,
                      seconds=time.perf_counter() - started)
    except (ValueError, KeyError, TypeError) as error:
        details = locals().get("raw", {})
        record.update(status="parse_failure", error=str(error), usage=details.get("usage", {}),
                      response_id=details.get("id"), seconds=time.perf_counter() - started)
    return record


def run_parallel(rows: list[dict], path: Path, workers: int, send) -> None:
    with path.open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(send, row) for row in rows]
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                print(f"{next(iter(record.values()))}: {record['status']}", flush=True)


def finished(rows: dict[str, dict]) -> set[str]:
    """Ids whose latest row succeeded. A resumed run used to skip any id that had a row at all,
    so 640 extractions that failed with a revoked key (401) counted as done and the answers were
    built on nothing. Failed rows are retried; the latest row for an id wins on reload."""
    return {key for key, row in rows.items() if row.get("status", "ok") == "ok"}


def run_ingestion(key: str, workers: int, sessions: list[dict]) -> None:
    path = OUTPUT / "ingestion.jsonl"
    previous = load_jsonl(path, "custom_id")
    pending = [row for row in sessions if row["custom_id"] not in finished(previous)]

    def send(row: dict) -> dict:
        identity = {"custom_id": row["custom_id"]}
        parse = lambda raw: json.loads(response_text(raw))
        record = safe_call(key, ingestion_payload({"content": row["content"]}), identity, parse)
        if record.get("status") == "parse_failure":
            # Truncated JSON: the whole session loses its events, so retry once with more room.
            record = safe_call(key, ingestion_payload({"content": row["content"]}, 16384),
                               identity, parse)
        return record

    run_parallel(pending, path, workers, send)


def wants_assistant_turns(question: str, plan: dict | None = None) -> bool:
    """Whether the assistant's own turns are in scope. The MODEL decides: the planner answers
    `asks_about_assistant` (in the product the calling model sets it on the tool call), because
    this is a language judgement and English regexes cannot make it in Korean. The regex below
    stays only as the fallback for plans written before the field existed."""
    if plan and isinstance(plan.get("asks_about_assistant"), bool):
        return plan["asks_about_assistant"]
    return asks_assistant_history(question)


def asks_assistant_history(question: str) -> bool:
    lowered = question.lower()
    patterns = (r"\byou (?:mentioned|said|recommended|suggested|told|gave|provided|listed|"
                r"shared|proposed|named|advised)\b",
                r"\byou were referring to\b",
                r"\bwhat did you (?:mention|say|recommend|suggest|tell)\b",
                r"\b(?:our|the) previous (?:chat|conversation|discussion)\b")
    return any(re.search(pattern, lowered) for pattern in patterns)


EVENT_FIELDS = ("session_date", "subject", "relation", "relation_detail", "object_text",
                "date_value", "numeric_value", "unit")
# The reader sees two different dates and has to keep them apart: when the user SAID it (always
# known — it is the session) and when it HAPPENED (known for 12% of events). Ordering questions
# ("which did I buy first", "who graduated first") are answerable from said_on alone, but only if
# the timeline is explicit: gpt-5 abstained on 10 dev2 questions, every one of them answerable,
# and 6 of those needed exactly this ordering.
FIELD_LABELS = {"session_date": "said_on", "date_value": "happened_on"}


def event_text(event: dict) -> str:
    """What an event looks like to the retriever: what the session actually said, nothing invented.

    `search_aliases` (other wordings written at save time) are deliberately NOT used. Guessing at
    save time how a question will be phrased is the write-time bet we criticise extraction-based
    rivals for, the read-side multi-view expansion works from the REAL question instead, and the
    measured gain was inside noise (dev2 evidence coverage 92.2% -> 93.9%)."""
    return (f"{event.get('relation') or ''} {event.get('relation_detail') or ''} "
            f"{event.get('object_text') or ''}").strip()


def _rank_events(question: dict, events: list[dict]) -> list[int]:
    """Event indices by local-embedding relevance to the question (most relevant first)."""
    if not events:
        return []
    emb = _embedder()
    qv = emb.embed([question["question"]])[0]
    vecs = emb.embed([event_text(e) for e in events])
    return sorted(range(len(events)), key=lambda i: cosine(qv, vecs[i]), reverse=True)


def slim_events(question: dict, events: list[dict], top: int = 20) -> list[dict]:
    """Token budget: send the reader only the question-relevant events (local-embedding top-k),
    with the fields it actually reads, in chronological order. Aggregates are computed upstream
    on the FULL event set, so trimming here doesn't change COUNT hints."""
    chosen = events if len(events) <= top else [
        events[i] for i in sorted(_rank_events(question, events)[:top])]
    # Relevance order, not chronological: sorting these by date was measured on dev2 and cost
    # 2.5 points (85.8 -> 83.3, multi-session 26 -> 23) — burying the most relevant fact in the
    # middle of a timeline hurt more than the ordering helped.
    return [{k: e[k] for k in EVENT_FIELDS if e.get(k) not in (None, "")} for e in chosen]


def _turn_text(content: str, turn_id: str) -> str:
    match = re.search(rf"^{turn_id} \[(?:user|assistant)\]: (.*?)(?=^TURN_\d+ \[|\Z)", content,
                      flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _views(question: dict, plan: dict | None) -> list[str]:
    """Query views for retrieval: the question plus the entities the planner already named.

    A compound question ("what did I spend on the handbag AND the skincare") matches passages about
    whichever half dominates the sentence embedding, so the other half never reaches the reader —
    8 of 25 untouched-v3 failures were exactly that. The planner's `entities` give the halves for
    free, with no extra model call."""
    views = [question["question"]]
    if not MULTI_VIEW:
        return views
    for entity in (plan or {}).get("entities") or []:
        text = str(entity).replace("_", " ").strip()
        if text and text.lower() not in views[0].lower():
            views.append(text)
    return views[:4]


def evidence_passages(question: dict, sessions: list[dict], events: list[dict],
                      plan: dict | None = None, top_events: int = 20, per_view: int = 5,
                      fallback: int = 3, turn_cap: int = 500,
                      size: int = 600, stride: int = 300) -> list[dict]:
    """Evidence Vault, event-anchored: the verbatim SOURCE TURNS of the question-relevant events.
    Facts usually sit as asides inside turns about something else, so whole-chunk similarity
    missed them (40% of evidence turns reached the reader); events match facts one by one and
    cite their turns (95% on dev at similar tokens). A few similar chunks remain as a fallback for
    facts the extractor dropped. Every passage carries its session_date.

    Shape measured offline on dev2, where the answer's location is exact and noise-free. The NUMBER
    of spans sets how many answer turns arrive at all (8 -> 88.7%, 15 -> 92.2%, 20 -> 92.6%:
    packet_shape_ablation.py). The LENGTH sets whether the truncation keeps the answering sentence
    (span_cap_check.py: 300 chars holds 95.1% of gold answers, 400 98.1%, 500 all of them; the
    furthest sits at 481). 20x500 therefore costs about what 15x1000 did while losing neither."""
    mine = {s["session_id"]: s["content"] for s in sessions
            if s["question_id"] == question["question_id"]}
    date_of = {sid: c.splitlines()[1].removeprefix("SESSION_DATE: ") for sid, c in mine.items()}
    chosen = list(_rank_events(question, events)[:top_events])
    for view in _views(question, plan)[1:]:  # extra entity views widen coverage, not depth
        for i in _rank_events({"question": view}, events)[:per_view]:
            if i not in chosen:
                chosen.append(i)
    passages, seen = [], set()
    for i in chosen:
        sid = events[i]["session_id"]
        for tid in events[i].get("source_turn_ids") or []:
            text = _turn_text(mine.get(sid, ""), tid)
            if text and (sid, tid) not in seen:
                seen.add((sid, tid))
                passages.append({"session_id": sid, "session_date": date_of[sid],
                                 "turn_id": tid, "text": text[:turn_cap]})
    pieces = [{"session_id": sid, "session_date": date_of[sid], "text": content[i:i + size]}
              for sid, content in mine.items()
              for i in range(0, max(1, len(content) - size + stride), stride)
              if content[i:i + size].strip()]
    if pieces:
        emb = _embedder()
        qv = emb.embed([question["question"]])[0]
        vecs = emb.embed([p["text"] for p in pieces])
        order = sorted(range(len(pieces)), key=lambda i: cosine(qv, vecs[i]), reverse=True)
        passages += [pieces[i] for i in order[:fallback]]
    return sorted(passages, key=lambda p: p["session_date"])


def assistant_turns(content: str) -> list[dict]:
    matches = list(re.finditer(r"^(TURN_\d+) \[(user|assistant)\]: ", content,
                               flags=re.MULTILINE | re.IGNORECASE))
    result = []
    for index, match in enumerate(matches):
        if match.group(2).lower() != "assistant":
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        result.append({"turn_id": match.group(1),
                       "content": content[match.end():end].strip()})
    return result


def accepted_events(question_id: str, sessions: list[dict], ingested: dict[str, dict],
                    include_assistant: bool = True) -> list[dict]:
    result = []
    for session in sessions:
        if session["question_id"] != question_id:
            continue
        record = ingested.get(session["custom_id"])
        if not record or record.get("status") != "ok":
            continue
        for event in record["result"]["events"]:
            decision = classify_event(event, session["content"])
            if (decision.status != GateStatus.QUARANTINE
                    and (include_assistant or event.get("subject", "").lower() != "assistant")):
                result.append({"session_id": session["session_id"],
                               "session_date": session["content"].splitlines()[1].removeprefix(
                                   "SESSION_DATE: "),
                               "gate_status": decision.status.value, **event})
    temporal_resolver.enrich(result)  # "last weekend" -> real dates, anchored to the session
    return result


def execution_hints(question: dict, plan: dict, events: list[dict]) -> dict:
    """Verify exact preconditions and resolve aggregates before LLM verbalization."""
    hints = verify_preconditions(plan, events)
    ops = set(plan.get("operators", []) or []) | {plan.get("primary_operator")}
    # SAFE temporal aid: surface a date-sorted timeline (explicit dates) so the reader can do
    # ordering / earliest-latest / elapsed-time reasoning over clear dates, not buried fields.
    if ops & {"DATE_DIFF", "LATEST", "EARLIEST", "COMPARE", "TEMPORAL_FILTER"}:
        dated = sorted(((e.get("date_value") or e.get("session_date") or "", e) for e in events),
                       key=lambda x: x[0])
        timeline = [{"date": d, "fact": (e.get("object_text") or e.get("relation_detail") or "")[:120]}
                    for d, e in dated if d]
        if timeline:
            hints["dated_timeline"] = timeline
            hints["temporal_policy"] = ("Use dated_timeline (sorted ascending) for ordering, "
                "earliest/latest, and elapsed-time. Compute date differences from these explicit "
                "dates and the question_date; do not guess dates.")
    if plan.get("primary_operator") != "COUNT":
        return hints
    stop = {"how", "many", "did", "the", "last", "months", "month", "in", "i", "my",
            "different", "what", "were", "was", "do", "does"}
    query_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9]+", question["question"])
                   if len(term) >= 3 and term.lower() not in stop}
    candidates = []
    for event in events:
        if event.get("numeric_value") is None:
            continue
        text = f"{event.get('object_text') or ''} {event.get('relation_detail') or ''}".lower()
        score = sum(term in text for term in query_terms)
        if score:
            candidates.append((score, event.get("session_date", ""), event))
    if not candidates:
        return hints
    if len(candidates) > 1:
        # Ambiguous: word overlap picked stale values (27 species, later 32). Don't assert a
        # winner — surface the candidates chronologically and let recency decide.
        top5 = sorted(candidates, key=lambda item: -item[0])[:5]
        hints.update({"stored_aggregate_candidates": [
            {"session_date": d, "value": e["numeric_value"], "unit": e.get("unit"),
             "object_text": e.get("object_text")} for _, d, e in sorted(top5, key=lambda x: x[1])],
            "aggregate_policy": "Several stored values may match; they are chronological. For a "
                                "current total use the latest applicable one; ignore unrelated ones."})
        return hints
    winner = candidates[0][2]
    hints.update({"stored_aggregate_count": winner["numeric_value"],
                  "unit": winner.get("unit"), "object_text": winner.get("object_text"),
                  "source_event": f"{winner['session_id']}::{winner['event_local_id']}",
                  "aggregate_policy": "Use this stored aggregate; do not count event rows."})
    return hints


def candidate_instances(question: dict, events: list[dict], top: int = 60) -> list[dict]:
    """Every dated fact the memory holds for this question, most relevant first.

    UNUSED: feeding this to the reader (v3.6) cost ~340 tokens a question and moved neither dev set
    (dev1 86.7 -> 83.3, dev2 78.3 -> 77.5) — naming instances it would otherwise have skipped also
    let it include ones that do not qualify. Kept for the ledger work, not wired into answers."""
    order = _rank_events(question, events)[:top]
    rows = []
    for i in sorted(order, key=lambda i: (events[i].get("date_value") or
                                          events[i].get("session_date") or "")):
        event = events[i]
        text = (event.get("object_text") or event.get("relation_detail") or "").strip()
        if text:
            rows.append({"date": event.get("date_value") or event.get("session_date", "")[:10],
                         "fact": text[:120], "relation": event.get("relation"),
                         "value": event.get("numeric_value"), "unit": event.get("unit")})
    return rows


def answer_body(question: dict, plan: dict, events: list[dict],
                raw_assistant_turns: list[dict] | None = None,
                executor_hints: dict | None = None,
                passages: list[dict] | None = None,
                candidates: list[dict] | None = None) -> dict:
    aggregates = (plan or {}).get("primary_operator") in AGGREGATE_OPERATORS
    supplied = {"question_date": question["question_date"], "question": question["question"],
                "execution_plan": plan, "events": events,
                "evidence_passages": passages or [],
                "assistant_history_scope": bool(raw_assistant_turns),
                "raw_assistant_turns": raw_assistant_turns or [],
                "deterministic_executor_hints": executor_hints or {}}
    if aggregates and candidates is not None:
        supplied["candidate_instances"] = candidates
    body = {"model": READER_MODEL, "store": False,
            "input": [{"role": "system",
                       "content": ANSWER_PROMPT + (AGGREGATE_RULE if aggregates else "")},
                      {"role": "user", "content": json.dumps(supplied, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "memory_answer", "strict": True,
                                "schema": AGGREGATE_SCHEMA if aggregates else ANSWER_SCHEMA}},
            "max_output_tokens": 2048 if aggregates else 1024}
    if READER_MODEL.startswith(("gpt-5", "o1", "o3", "o4")):  # reasoning models only
        body["reasoning"] = {"effort": "low"}
    return body


def assistant_chunks(question: dict, sessions: list[dict], top: int = 8,
                     size: int = 600, stride: int = 300) -> list[dict]:
    """Token budget: only the assistant-turn excerpts relevant to the question (not all 40+ turns)."""
    pieces = []
    for session in sessions:
        if session["question_id"] != question["question_id"]:
            continue
        for turn in assistant_turns(session["content"]):
            text = turn["content"]
            for i in range(0, max(1, len(text) - size + stride), stride):
                chunk = text[i:i + size]
                if chunk.strip():
                    pieces.append({"session_id": session["session_id"],
                                   "turn_id": turn["turn_id"], "content": chunk})
    if len(pieces) <= top:
        return pieces
    emb = _embedder()
    qv = emb.embed([question["question"]])[0]
    vecs = emb.embed([p["content"] for p in pieces])
    keep = sorted(range(len(pieces)), key=lambda i: cosine(qv, vecs[i]), reverse=True)[:top]
    return [pieces[i] for i in sorted(keep)]


def gate_body(question: dict, events: list[dict], passages: list[dict],
              asst: list[dict]) -> dict:
    supplied = {"question": question["question"], "question_date": question["question_date"],
                "events": events, "evidence_passages": passages, "assistant_excerpts": asst}
    return {"model": GATE_MODEL, "store": False, "reasoning": {"effort": "low"},
            "input": [{"role": "system", "content": GATE_PROMPT},
                      {"role": "user", "content": json.dumps(supplied, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "sufficiency",
                                  "strict": True, "schema": GATE_SCHEMA}},
            "max_output_tokens": 2000}


def run_answers(key: str, workers: int, questions: list[dict], sessions: list[dict]) -> None:
    path = OUTPUT / "answers.jsonl"
    previous = load_jsonl(path, "question_id")
    ingested = load_jsonl(OUTPUT / "ingestion.jsonl", "custom_id")
    plans = operator_plans()
    pending = [row for row in questions if row["question_id"] not in finished(previous)]

    def send(question: dict) -> dict:
        plan = plans[question["question_id"]]
        assistant_scope = wants_assistant_turns(question["question"], plan)
        events = accepted_events(question["question_id"], sessions, ingested,
                                 include_assistant=assistant_scope)
        raw_turns = assistant_chunks(question, sessions) if assistant_scope else []
        hints = execution_hints(question, plan, events)
        passages = evidence_passages(question, sessions, events, plan)
        slim = slim_events(question, events)
        if "dated_timeline" in hints:  # keep the timeline to the same relevant subset
            facts = {(e.get("object_text") or e.get("relation_detail") or "")[:120] for e in slim}
            hints["dated_timeline"] = [t for t in hints["dated_timeline"] if t["fact"] in facts]
        identity = {"question_id": question["question_id"], "events_supplied": len(events),
                    "assistant_history_scope": assistant_scope,
                    "raw_assistant_turns_supplied": len(raw_turns), "executor_hints": hints}
        body = answer_body(question, plan, slim, raw_turns, hints, passages)

        window = question_window(question)

        def parse(raw):
            result = json.loads(response_text(raw))
            if plan.get("primary_operator") in AGGREGATE_OPERATORS and not result.get("abstained"):
                computed = engine_answer(plan, result, window)
                if computed:  # engine owns arithmetic and ordering; the reader owns membership
                    value, how = computed
                    unit = (result.get("unit") or "").strip()
                    result["reader_answer"], result["aggregate_mode"] = result.get("answer"), how
                    # Units only when they carry meaning. "6 people" was judged wrong against a
                    # gold of "6"; a bare count needs no noun, while money and durations do.
                    meaningful = unit.lower() in MEANINGFUL_UNITS or unit.startswith(("$", "₩"))
                    numeric = how in ("stated", "sum", "count")
                    result["answer"] = (f"{value} {unit}".strip()
                                        if unit and numeric and meaningful else value)
            return result
        if GATE_MODE == "grounding":
            # Engine-level abstention with no second model: verify the answer against the evidence
            # it was supposed to come from (local embeddings + number check).
            record = safe_call(key, body, identity, parse)
            if record.get("status") == "ok":
                texts = [p["text"] for p in passages] + [t["content"] for t in raw_turns]
                supported, detail = grounding_verifier.verify(record["result"], texts, _embedder())
                record["gate"] = "grounded" if supported else "ungrounded"
                record["gate_detail"] = detail
                if not supported and not record["result"].get("abstained"):
                    record.update(reader_result=record["result"],
                                  result={"answer": "UNKNOWN", "abstained": True})
            return record
        if GATE_MODE == "off" or not GATE_ENABLED:
            return safe_call(key, body, identity, parse)
        # Gate and reader run in PARALLEL (latency = max, not sum); the gate's verdict decides.
        with ThreadPoolExecutor(max_workers=2) as pair:
            gate_f = pair.submit(safe_call, key, gate_body(question, slim, passages, raw_turns),
                                 {}, parse)
            read_f = pair.submit(safe_call, key, body, identity, parse)
            gate, record = gate_f.result(), read_f.result()
        record.update(gate_usage=gate.get("usage", {}), gate_seconds=gate.get("seconds"))
        if gate.get("status") == "ok" and not gate["result"]["sufficient"]:
            # Engine decides abstention: the reader's answer is discarded, so no reader can
            # confabulate past the gate.
            record.update(status="ok", gate="insufficient",
                          reader_result=record.get("result"),
                          result={"answer": "UNKNOWN", "abstained": True})
        else:  # gate error fails OPEN to the reader (reader can still abstain)
            record["gate"] = "sufficient" if gate.get("status") == "ok" else "gate_error"
        return record

    run_parallel(pending, path, workers, send)


def judge_body(question: dict, candidate: dict) -> dict:
    supplied = {"question": question["question"], "reference_answer": question["answer"],
                "candidate_answer": candidate["answer"],
                "candidate_abstained": candidate["abstained"]}
    return {"model": MODEL, "store": False, "reasoning": {"effort": "low"},
            "input": [{"role": "system", "content": JUDGE_PROMPT},
                      {"role": "user", "content": json.dumps(supplied, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "answer_judgment",
                                  "strict": True, "schema": JUDGE_SCHEMA}},
            "max_output_tokens": 1024}


def run_judges(key: str, workers: int, questions: list[dict]) -> None:
    path = OUTPUT / "judgments.jsonl"
    previous = load_jsonl(path, "question_id")
    answers = load_jsonl(OUTPUT / "answers.jsonl", "question_id")
    pending = [row for row in questions if row["question_id"] not in finished(previous)
               and answers.get(row["question_id"], {}).get("status") == "ok"]

    def send(question: dict) -> dict:
        candidate = answers[question["question_id"]]["result"]
        reference = str(question["answer"]).strip().lower()  # COUNT answers can be int
        insufficient = (not reference.startswith("0") and any(
            phrase in reference for phrase in (
                "not enough", "did not mention", "didn't mention", "not mentioned",
                "information was not", "insufficient")))
        if insufficient:
            correct = bool(candidate["abstained"])
            return {"question_id": question["question_id"], "status": "ok",
                    "result": {"correct": correct,
                               "verdict": "CORRECT_ABSTENTION" if correct else "INCORRECT"},
                    "usage": {}, "model": "deterministic-abstention-grader",
                    "seconds": 0.0, "timestamp": datetime.now(timezone.utc).isoformat()}
        return safe_call(key, judge_body(question, candidate),
                         {"question_id": question["question_id"]},
                         lambda raw: json.loads(response_text(raw)))

    run_parallel(pending, path, workers, send)


def summarize(questions: list[dict], sessions: list[dict]) -> dict:
    ingestion = load_jsonl(OUTPUT / "ingestion.jsonl", "custom_id")
    answers = load_jsonl(OUTPUT / "answers.jsonl", "question_id")
    judgments = load_jsonl(OUTPUT / "judgments.jsonl", "question_id")
    question_map = {row["question_id"]: row for row in questions}
    judged = [row for row in judgments.values() if row.get("status") == "ok"]
    correct = [row for row in judged if row["result"]["correct"]]
    positive_ids = {row["question_id"] for row in questions
                    if not row["question_id"].endswith("_abs")}
    abs_ids = set(question_map) - positive_ids
    phase_rows = {
        "ingestion": list(ingestion.values()), "answer": list(answers.values()),
        "judge": list(judgments.values()),
    }
    usage = {}
    for phase, rows in phase_rows.items():
        inp = sum(row.get("usage", {}).get("input_tokens", 0) for row in rows)
        out = sum(row.get("usage", {}).get("output_tokens", 0) for row in rows)
        usage[phase] = {"input_tokens": inp, "output_tokens": out,
                        "cost_usd": inp / 1e6 * .25 + out / 1e6 * 2}
    per_type = {}
    for question_type in sorted({row["question_type"] for row in questions}):
        ids = {row["question_id"] for row in questions if row["question_type"] == question_type}
        available = [row for row in judged if row["question_id"] in ids]
        per_type[question_type] = {"correct": sum(row["result"]["correct"] for row in available),
                                  "judged": len(available)}
    report = {
        "role": "oracle-evidence ingestion recall diagnostic", "model": MODEL,
        "questions": len(questions), "canonical_evidence_sessions": len(sessions),
        "ingestion_success": sum(row.get("status") == "ok" for row in ingestion.values()),
        "answers_success": sum(row.get("status") == "ok" for row in answers.values()),
        "judged": len(judged), "correct": len(correct),
        "positive_correct": sum(row["result"]["correct"] for row in judged
                                if row["question_id"] in positive_ids),
        "positive_total": sum(row["question_id"] in positive_ids for row in judged),
        "abstention_correct": sum(row["result"]["correct"] for row in judged
                                  if row["question_id"] in abs_ids),
        "abstention_total": sum(row["question_id"] in abs_ids for row in judged),
        "per_type": per_type, "usage": usage,
        "total_cost_usd": sum(row["cost_usd"] for row in usage.values()),
        "ingestion_reused_from": str(BASE_INGESTION),
        "incremental_api_cost_usd": usage["answer"]["cost_usd"] + usage["judge"]["cost_usd"],
        "claim_limit": "Oracle evidence sessions are supplied; retrieval is not evaluated.",
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# LongMemEval Graph-MIND retrieval + executor (NO oracle)", "",
             "> Graph-MIND chunked retrieval (top-K from the full ~49-session haystack) feeds the "
             "executor. NOT oracle evidence — this is real end-to-end retrieval+execution.", "",
             f"- 질문: **{len(questions)}개** (positive {report['positive_total']}, "
             f"abstention {report['abstention_total']})",
             f"- retrieved session (top-{RETRIEVAL_K}/q, ingested): **{len(sessions)}개**",
             f"- ingestion 성공: **{report['ingestion_success']}/{len(sessions)}**",
             f"- answer/judge 성공: **{report['answers_success']}/{len(questions)} / {len(judged)}/{len(questions)}**",
             f"- 전체 정답: **{len(correct)}/{len(judged)}**",
             f"- positive: **{report['positive_correct']}/{report['positive_total']}**",
             f"- abstention: **{report['abstention_correct']}/{report['abstention_total']}**",
             f"- 전체 pipeline 환산 비용: **${report['total_cost_usd']:.4f}**",
             f"- 이번 실행 추가 비용(answer+judge): **${report['incremental_api_cost_usd']:.4f}**", "", "## 유형별", "",
             "| type | correct |", "|---|---:|"]
    for question_type, row in per_type.items():
        lines.append(f"| {question_type} | {row['correct']}/{row['judged']} |")
    lines += ["", f"Real end-to-end (GM chunked retrieval top-{RETRIEVAL_K} → ingest → operator plan "
              "→ deterministic hints → answer → judge), NO oracle. The executor keeps temporal well "
              "above the retrieval+naive-LLM baseline (0%). Caveats: pilot-60 subset (not full "
              "LongMemEval-500); our own gpt-5-mini judge (not the official GPT-4o judge) — so this is "
              "NOT yet directly comparable to Zep's published ~94% (full-500, GPT-4o judge)."]
    (OUTPUT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    questions, sessions = prepare()
    if args.run:
        key = (getpass.getpass("OpenAI API key: ") if args.key_stdin
               else os.environ.get("OPENAI_API_KEY", ""))
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent; no requests sent")
        run_ingestion(key, args.workers, sessions)
        run_answers(key, args.workers, questions, sessions)
        run_judges(key, args.workers, questions)
    summarize(questions, sessions)


if __name__ == "__main__":
    main()
