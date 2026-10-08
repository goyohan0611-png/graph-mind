import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json

from conversation_memory import ConversationMemoryStore
from local_brain import LocalBrainStore
from semantic_llm_extractor import (OpenAIResponsesSemanticExtractor,
                                    SemanticLLMIngestion, verify_candidate)


T = "2026-09-16T12:00:00"


def turn(turn_id, role, content, ordinal=1):
    return {"turn_id": turn_id, "client": "codex", "client_session_id": "s1",
        "scope": "project:test", "cwd": None, "happened_at": T, "known_at": T,
        "role": role, "content_redacted": content, "raw_sha256": turn_id,
        "redaction_count": 0, "source_path": "test.jsonl", "source_ordinal": ordinal}


class FakeExtractor:
    model = "fake-semantic-model"
    def __init__(self, proposals):
        self.proposals = list(proposals); self.calls = 0
    def propose(self, content):
        value = self.proposals[self.calls]; self.calls += 1
        return value


class SemanticLLMExtractorTests(unittest.TestCase):
    def test_openai_adapter_uses_stateless_structured_output(self):
        captured = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self):
                return json.dumps({"output": [{"type": "message", "content": [{
                    "type": "output_text", "text": '{"candidates": []}'}]}]}).encode()
        def fake_urlopen(message, timeout):
            captured["body"] = json.loads(message.data.decode())
            captured["timeout"] = timeout
            return Response()
        with patch("semantic_llm_extractor.request.urlopen", fake_urlopen):
            extractor = OpenAIResponsesSemanticExtractor(
                "test-model", api_key="test-key", timeout=7)
            result = extractor.propose("작은 기억")
        self.assertEqual(result, {"candidates": []})
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(captured["body"]["text"]["format"]["type"], "json_schema")
        self.assertTrue(captured["body"]["text"]["format"]["strict"])
        self.assertEqual(captured["timeout"], 7)

    def test_small_exact_detail_is_promoted_and_remains_source_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            text = "어제 문구점에서 빨간 노트를 하나 샀어"
            start, end = text.index("빨간"), len(text)
            with ConversationMemoryStore(db) as store:
                store.record(turn("t1", "user", text))
            extractor = FakeExtractor([{"candidates": [{"memory_type": "PERSONAL_DETAIL",
                "quote_start": start, "quote_end": end}]}])
            with SemanticLLMIngestion(db, now=lambda: T) as ingestion:
                first = ingestion.process_new(extractor); second = ingestion.process_new(extractor)
            with LocalBrainStore(db) as brain:
                recalled = brain.recall("빨간 노트", as_of=T)
            self.assertEqual(first["promoted_count"], 1)
            self.assertEqual(first["api_calls"], 1)
            self.assertEqual(second["status"], "NO_NEW_TURNS")
            memory = recalled["matches"][0]
            self.assertEqual(memory["content"], "빨간 노트를 하나 샀어")
            self.assertIn("#chars=", memory["provenance"]["source_ref"])

    def test_hallucinated_or_question_candidate_fails_closed(self):
        source = {"role": "user", "content_redacted": "검은 가방을 샀어?"}
        candidate = {"memory_type": "PERSONAL_DETAIL", "quote_start": 0,
                     "quote_end": len(source["content_redacted"])}
        self.assertEqual(verify_candidate(source, candidate)[1], "QUESTION_TURN")
        bad = {"memory_type": "FACT", "quote_start": 0, "quote_end": 999}
        source["content_redacted"] = "오늘 비가 왔어"
        self.assertEqual(verify_candidate(source, bad)[1], "SOURCE_SPAN_OUT_OF_RANGE")

    def test_assistant_and_redacted_turns_never_reach_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn("t1", "assistant", "사용자는 빨간색을 좋아한다", 1))
                redacted = turn("t2", "user", "내 키는 [REDACTED_API_KEY]야", 2)
                redacted["redaction_count"] = 1
                store.record(redacted)
            extractor = FakeExtractor([])
            with SemanticLLMIngestion(db, now=lambda: T) as ingestion:
                result = ingestion.process_new(extractor)
            self.assertEqual(result["processed_turns"], 2)
            self.assertEqual(result["api_calls"], 0)
            self.assertEqual(result["promoted_count"], 0)


if __name__ == "__main__":
    unittest.main()
