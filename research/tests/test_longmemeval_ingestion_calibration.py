import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest

from longmemeval_ingestion_calibration import (
    RELATIONS, bad_role_provenance, payload, response_text,
    role_played_user_turn_ids,
)


class IngestionCalibrationTests(unittest.TestCase):
    def test_payload_contains_session_but_no_question(self):
        body = payload({"content": "SESSION_ID: s1\nTURN_000 [user]: I moved."})
        encoded = str(body)
        self.assertIn("SESSION_ID: s1", encoded)
        self.assertNotIn("answer_session_ids", encoded)
        self.assertNotIn("question_date", encoded)
        self.assertEqual(body["reasoning"]["effort"], "low")
        self.assertEqual(body["max_output_tokens"], 8192)

    def test_response_text_and_relation_contract(self):
        raw = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"events":[]}'}]}]}
        self.assertEqual(response_text(raw), '{"events":[]}')
        self.assertIn("HAS_COST", RELATIONS)
        self.assertIn("HAS_DURATION", RELATIONS)

    def test_user_memory_requires_user_source(self):
        content = "TURN_000 [user]: I take the train.\nTURN_001 [assistant]: I will buy a pass."
        events = [
            {"event_local_id": "e1", "subject": "user", "source_turn_ids": ["TURN_000"]},
            {"event_local_id": "e2", "subject": "user", "source_turn_ids": ["TURN_001"]},
            {"event_local_id": "e3", "subject": "assistant", "source_turn_ids": ["TURN_001"]},
        ]
        self.assertEqual(bad_role_provenance(events, content), ["e2"])

    def test_explicit_role_play_is_quarantinable(self):
        content = ("TURN_000 [user]: Please respond as the user.\n"
                   "TURN_001 [assistant]: I will buy a pass.\n"
                   "TURN_002 [user]: That sounds good.\n"
                   "TURN_003 [assistant]: I will pack coffee.")
        self.assertEqual(role_played_user_turn_ids(content), {"TURN_001", "TURN_003"})


if __name__ == "__main__":
    unittest.main()
