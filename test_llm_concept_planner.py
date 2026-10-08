from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest

from concept_plan_benchmark_v071 import prepare, capture, _records
from llm_concept_planner import OpenAIResponsesConceptPlanner, validate_plan


class ConceptPlannerTests(unittest.TestCase):
    def test_payload_is_target_blind_stateless_structured_output(self):
        planner = OpenAIResponsesConceptPlanner("test-model", api_key="test-key")
        payload = planner.request_payload("전에 정한 조합이 뭐였지?")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"], [{"role": "user", "content":
                                             "전에 정한 조합이 뭐였지?"}])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertNotIn("expected", serialized)
        self.assertNotIn("memory_contents", serialized)

    def test_plan_validation_fails_closed(self):
        self.assertEqual(validate_plan({"status": "abstain", "concept_cues": []}),
                         {"status": "abstain", "concept_cues": []})
        with self.assertRaises(ValueError):
            validate_plan({"status": "plan", "concept_cues": ["one"]})
        with self.assertRaises(ValueError):
            validate_plan({"status": "abstain", "concept_cues": ["extra", "cue"]})

    def test_invalid_model_output_becomes_safe_abstention(self):
        response = {"id": "r1", "model": "test-model", "usage": {
            "input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            "output": [{"type": "message", "content": [{"type": "output_text",
                "text": json.dumps({"status": "plan", "concept_cues": ["one"]})}]}]}
        with patch("llm_concept_planner.request.urlopen",
                   return_value=io.BytesIO(json.dumps(response).encode())):
            plan = OpenAIResponsesConceptPlanner(
                "test-model", api_key="test-key").plan("질문")
        self.assertEqual(plan["status"], "abstain")
        self.assertEqual(plan["concept_cues"], [])
        self.assertTrue(plan["validation_error"].startswith("INVALID_MODEL_OUTPUT:"))

    def test_frozen_capture_reads_public_questions_and_never_writes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            cases = Path(directory) / "cases.json"
            cases.write_text(json.dumps([{"id": "q1", "style": "VAGUE",
                "query": "무슨 설계였지?", "expected": ["secret-target"],
                "concept_cues": ["oracle secret"]}]), encoding="utf-8")
            manifest = prepare(root, cases, "test-model")
            public = (root / "questions.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-target", public)
            self.assertNotIn("oracle secret", public)
            response = {"id": "r1", "model": "test-model", "usage": {
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                "output": [{"type": "message", "content": [{"type": "output_text",
                    "text": json.dumps({"status": "plan", "concept_cues":
                                       ["memory architecture", "design decision"]})}]}]}
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only-secret"}), \
                    patch("llm_concept_planner.request.urlopen",
                          return_value=io.BytesIO(json.dumps(response).encode())):
                result = capture(root)
            self.assertEqual(result["remaining"], 0)
            stored = (root / "responses.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("test-only-secret", stored)
            self.assertEqual(manifest["planner_never_sees"],
                             ["style", "expected", "concept_cues", "memory_contents"])

    def test_duplicate_success_keeps_first_plan_without_cherry_picking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "responses.jsonl"
            rows = [
                {"id": "q1", "status": "api_error"},
                {"id": "q1", "status": "ok", "plan": {"concept_cues": ["first", "plan"]}},
                {"id": "q1", "status": "ok", "plan": {"concept_cues": ["later", "plan"]}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                            encoding="utf-8")
            self.assertEqual(_records(path)["q1"]["plan"]["concept_cues"],
                             ["first", "plan"])


if __name__ == "__main__":
    unittest.main()
