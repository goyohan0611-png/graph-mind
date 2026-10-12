"""LLM candidate extraction with deterministic source-span verification."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib import error, request
import hashlib
import json
import os
import sqlite3

from local_brain import LocalBrainStore


EXTRACTOR_VERSION = "semantic-llm-extractor-v0.4"
MEMORY_TYPES = frozenset({
    "FACT", "PREFERENCE", "DECISION", "PROJECT_PRINCIPLE", "PERSONAL_DETAIL",
    "PROJECT_DETAIL",
})
CANDIDATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"candidates": {"type": "array", "maxItems": 5, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
            "quote_start": {"type": "integer", "minimum": 0},
            "quote_end": {"type": "integer", "minimum": 1},
        },
        "required": ["memory_type", "quote_start", "quote_end"],
    }}},
    "required": ["candidates"],
}
INSTRUCTIONS = """You identify potentially useful long-term memories in one user utterance.
Return only exact character spans from the supplied text. Include small personal or project
details when they may help a later conversation. Do not extract requests to perform a current
task, general questions, secrets, credentials, or anything not stated by the user. Use Python
string character offsets: quote_start inclusive, quote_end exclusive. Return no more than five
candidates. The caller independently verifies every span and may reject all output."""


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_candidate(turn, candidate):
    if turn["role"] != "user":
        return None, "NON_USER_TURN"
    content = turn["content_redacted"]
    if "[REDACTED" in content:
        return None, "REDACTED_CONTENT"
    if content.rstrip().endswith(("?", "？")):
        return None, "QUESTION_TURN"
    if not isinstance(candidate, dict) or set(candidate) != {
            "memory_type", "quote_start", "quote_end"}:
        return None, "INVALID_CANDIDATE_SCHEMA"
    memory_type = candidate.get("memory_type")
    start, end = candidate.get("quote_start"), candidate.get("quote_end")
    if memory_type not in MEMORY_TYPES:
        return None, "INVALID_MEMORY_TYPE"
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(content):
        return None, "SOURCE_SPAN_OUT_OF_RANGE"
    quote = content[start:end]
    if len(quote.strip()) < 3:
        return None, "SOURCE_SPAN_TOO_SHORT"
    return {"memory_type": memory_type, "quote_start": start,
            "quote_end": end, "quote": quote}, "SOURCE_SPAN_VERIFIED"


class OpenAIResponsesSemanticExtractor:
    """Optional no-SDK adapter. Credentials are read from process environment only."""

    def __init__(self, model, *, api_key=None, timeout=45):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("SEMANTIC_EXTRACTOR_MODEL_REQUIRED")
        self.model = model.strip()
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY_REQUIRED")
        self.timeout = timeout

    def propose(self, content):
        body = {"model": self.model, "store": False,
            "instructions": INSTRUCTIONS, "input": content,
            "max_output_tokens": 800,
            "text": {"format": {"type": "json_schema",
                "name": "graph_mind_memory_candidates", "strict": True,
                "schema": CANDIDATE_SCHEMA}}}
        message = request.Request("https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"})
        try:
            with request.urlopen(message, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise RuntimeError("OPENAI_RESPONSES_HTTP_ERROR:" + str(exc.code)) from None
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("OPENAI_RESPONSES_TRANSPORT_ERROR:" +
                               type(exc).__name__) from None
        texts = []
        for item in payload.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if not texts:
            raise RuntimeError("OPENAI_RESPONSES_OUTPUT_TEXT_MISSING")
        result = json.loads("".join(texts))
        if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
            raise RuntimeError("OPENAI_RESPONSES_CANDIDATES_INVALID")
        return result


class SemanticLLMIngestion:
    def __init__(self, path, *, now=None):
        self.path = Path(path)
        self.now = now or _now
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_llm_audit(
          turn_id TEXT PRIMARY KEY,
          extractor_version TEXT NOT NULL,
          model TEXT NOT NULL,
          disposition TEXT NOT NULL,
          proposed_count INTEGER NOT NULL,
          promoted_count INTEGER NOT NULL,
          rejection_reasons_json TEXT NOT NULL,
          processed_at TEXT NOT NULL);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def _turns(self, limit):
        return self.db.execute("""SELECT t.* FROM conversation_turns t
          LEFT JOIN semantic_llm_audit a ON a.turn_id=t.turn_id
          WHERE a.turn_id IS NULL ORDER BY t.revision LIMIT ?""", (limit,)).fetchall()

    def process_new(self, extractor, *, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("INVALID_LLM_INGESTION_LIMIT")
        model = getattr(extractor, "model", type(extractor).__name__)
        processed, promoted, api_calls = 0, [], 0
        for turn in self._turns(limit):
            candidates, rejections = [], []
            if turn["role"] != "user":
                rejections.append("NON_USER_TURN")
            elif "[REDACTED" in turn["content_redacted"]:
                rejections.append("REDACTED_CONTENT")
            else:
                proposal = extractor.propose(turn["content_redacted"])
                api_calls += 1
                candidates = proposal.get("candidates", []) if isinstance(proposal, dict) else []
                if not isinstance(candidates, list) or len(candidates) > 5:
                    candidates = []
                    rejections.append("INVALID_CANDIDATE_LIST")
            turn_promoted = 0
            for ordinal, candidate in enumerate(candidates):
                verified, reason = verify_candidate(turn, candidate)
                if not verified:
                    rejections.append(reason)
                    continue
                binding = f"{turn['turn_id']}\0{ordinal}\0{verified['memory_type']}\0{verified['quote_start']}\0{verified['quote_end']}"
                memory_id = "semantic-llm-" + _digest(binding)[:32]
                quote = verified["quote"]
                memory = {"memory_id": memory_id, "scope": turn["scope"],
                    "memory_type": verified["memory_type"],
                    "memory_class": {
                        "PREFERENCE": "PREFERENCE", "DECISION": "DECISION",
                        "FACT": "FACT", "PROJECT_PRINCIPLE": "STATE",
                    }.get(verified["memory_type"], "FACT"),
                    "modality": "CONVERSATION", "retention": "DURABLE",
                    "importance": 0.75, "confidence": 1.0,
                    "attributes": {"extractor_version": EXTRACTOR_VERSION,
                                   "model": str(model),
                                   "quote_start": verified["quote_start"],
                                   "quote_end": verified["quote_end"]},
                    "related_memory_ids": ["conversation-turn:" + turn["turn_id"]],
                    "title": f"{verified['memory_type']}: {quote[:80]}",
                    "content": quote, "effective_at": turn["happened_at"],
                    "known_at": self.now(), "actor": "user",
                    "tags": [verified["memory_type"], "llm-candidate-verified"],
                    "entities": [], "provenance": {
                        "source_type": "verified-conversation-span",
                        "source_ref": (f"conversation-turn:{turn['turn_id']}#chars="
                                       f"{verified['quote_start']}-{verified['quote_end']}")},
                    "supersedes_memory_id": None}
                with LocalBrainStore(self.path) as brain:
                    brain.remember(memory)
                promoted.append({"turn_id": turn["turn_id"], "memory_id": memory_id,
                    "memory_type": verified["memory_type"],
                    "quote_start": verified["quote_start"],
                    "quote_end": verified["quote_end"]})
                turn_promoted += 1
            disposition = "PROMOTED" if turn_promoted else "EVIDENCE_ONLY"
            with self.db:
                self.db.execute("""INSERT INTO semantic_llm_audit(
                  turn_id,extractor_version,model,disposition,proposed_count,
                  promoted_count,rejection_reasons_json,processed_at)
                  VALUES(?,?,?,?,?,?,?,?)""", (turn["turn_id"], EXTRACTOR_VERSION,
                    str(model), disposition, len(candidates), turn_promoted,
                    json.dumps(sorted(set(rejections))), self.now()))
            processed += 1
        return {"status": "LLM_TURNS_PROCESSED" if processed else "NO_NEW_TURNS",
                "processed_turns": processed, "api_calls": api_calls,
                "promoted_count": len(promoted), "promoted": promoted}
