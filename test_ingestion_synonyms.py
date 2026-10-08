import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import io
import json
import unittest
from unittest.mock import patch

from ingestion_synonyms import OpenAIResponsesAliasExtractor, validate_aliases


def _response(cues, status="plan"):
    body = {"status": status, "alias_cues": cues}
    return {"id": "r1", "model": "test-model",
            "usage": {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18},
            "output": [{"type": "message", "content": [{"type": "output_text",
                "text": json.dumps(body, ensure_ascii=False)}]}]}


class IngestionSynonymTests(unittest.TestCase):
    def test_payload_is_source_bound_stateless_structured_output(self):
        extractor = OpenAIResponsesAliasExtractor("test-model", api_key="test-key")
        payload = extractor.request_payload("engram associative v0.6 smoke latency")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        # only the record's own text is ever sent
        self.assertEqual(payload["input"][0]["content"],
                         "engram associative v0.6 smoke latency")

    def test_validation_requires_three_distinct_cues_and_dedups(self):
        self.assertEqual(validate_aliases({"status": "abstain", "alias_cues": []}),
                         {"status": "abstain", "alias_cues": []})
        with self.assertRaises(ValueError):
            validate_aliases({"status": "plan", "alias_cues": ["one", "two"]})
        with self.assertRaises(ValueError):
            validate_aliases({"status": "abstain", "alias_cues": ["x", "y", "z"]})
        got = validate_aliases({"status": "plan",
            "alias_cues": ["연상 검색", "associative recall", "연상 검색", "속도"]})
        self.assertEqual(got["alias_cues"], ["연상 검색", "associative recall", "속도"])

    def test_valid_bilingual_aliases_pass_through(self):
        with patch("ingestion_synonyms.request.urlopen",
                   return_value=io.BytesIO(json.dumps(
                       _response(["연상 검색", "associative recall", "지연 속도"]))
                       .encode())):
            out = OpenAIResponsesAliasExtractor(
                "test-model", api_key="test-key").extract("연상 기억 latency")
        self.assertEqual(out["status"], "plan")
        self.assertEqual(out["alias_cues"], ["연상 검색", "associative recall", "지연 속도"])
        self.assertIsNone(out["validation_error"])

    def test_invalid_model_output_becomes_safe_abstention(self):
        with patch("ingestion_synonyms.request.urlopen",
                   return_value=io.BytesIO(json.dumps(_response(["only", "two"])).encode())):
            out = OpenAIResponsesAliasExtractor(
                "test-model", api_key="test-key").extract("some text")
        self.assertEqual(out["status"], "abstain")
        self.assertEqual(out["alias_cues"], [])
        self.assertTrue(out["validation_error"].startswith("INVALID_MODEL_OUTPUT:"))

    def test_api_key_never_appears_in_result(self):
        with patch("ingestion_synonyms.request.urlopen",
                   return_value=io.BytesIO(json.dumps(
                       _response(["a b", "c d", "e f"])).encode())):
            out = OpenAIResponsesAliasExtractor(
                "test-model", api_key="super-secret-key").extract("text")
        self.assertNotIn("super-secret-key", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
