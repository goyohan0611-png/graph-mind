import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest

from ingestion_gate import GateStatus, classify_event


class IngestionGateTests(unittest.TestCase):
    def test_persists_supported_user_fact(self):
        content = "TURN_000 [user]: I spend $12 per day."
        event = {"subject": "user", "source_turn_ids": ["TURN_000"],
                 "value_type": "MONEY", "numeric_value": 12, "unit": "USD"}
        self.assertEqual(classify_event(event, content).status, GateStatus.PERSIST)

    def test_quarantines_explicit_role_play(self):
        content = ("TURN_000 [user]: Please respond as the user.\n"
                   "TURN_001 [assistant]: I will buy a train pass.")
        event = {"subject": "user", "source_turn_ids": ["TURN_001"],
                 "value_type": "TEXT"}
        decision = classify_event(event, content)
        self.assertEqual(decision.status, GateStatus.QUARANTINE)
        self.assertEqual(decision.reasons, ("EXPLICIT_ROLE_PLAY_BLOCK",))

    def test_quarantines_both_channels_after_role_switch(self):
        content = ("TURN_000 [user]: Please respond as the user.\n"
                   "TURN_001 [assistant]: I will buy a pass.\n"
                   "TURN_002 [user]: You should pack coffee.")
        event = {"subject": "user", "source_turn_ids": ["TURN_001", "TURN_002"],
                 "value_type": "TEXT", "object_text": "pack coffee"}
        self.assertEqual(
            classify_event(event, content).reasons, ("EXPLICIT_ROLE_PLAY_BLOCK",))

    def test_quarantines_missing_source(self):
        content = "TURN_000 [user]: I moved."
        event = {"subject": "user", "source_turn_ids": ["TURN_999"],
                 "value_type": "TEXT"}
        self.assertEqual(classify_event(event, content).status, GateStatus.QUARANTINE)

    def test_keeps_unparsed_range_as_evidence_only(self):
        content = "TURN_000 [assistant]: Plan for 2-3 hours."
        event = {"subject": "assistant", "source_turn_ids": ["TURN_000"],
                 "value_type": "DURATION", "numeric_value": None, "unit": "hours"}
        decision = classify_event(event, content)
        self.assertEqual(decision.status, GateStatus.EVIDENCE_ONLY)
        self.assertEqual(decision.reasons, ("MISSING_NUMERIC_VALUE",))

    def test_keeps_parsed_range_out_of_scalar_algebra(self):
        content = "TURN_000 [assistant]: Plan for 2-3 hours."
        event = {"subject": "assistant", "source_turn_ids": ["TURN_000"],
                 "value_type": "DURATION", "numeric_value": None,
                 "numeric_min": 2, "numeric_max": 3, "unit": "hours"}
        decision = classify_event(event, content)
        self.assertEqual(decision.status, GateStatus.EVIDENCE_ONLY)
        self.assertEqual(
            decision.reasons, ("RANGE_VALUE_UNSUPPORTED_BY_SCALAR_ALGEBRA",))

    def test_rejects_empty_text_and_boolean_values(self):
        content = "TURN_000 [user]: My cousin visited."
        text_event = {"subject": "user", "source_turn_ids": ["TURN_000"],
                      "value_type": "TEXT", "object_text": None}
        boolean_event = {"subject": "user", "source_turn_ids": ["TURN_000"],
                         "value_type": "BOOLEAN", "boolean_value": None}
        self.assertEqual(classify_event(text_event, content).status, GateStatus.EVIDENCE_ONLY)
        self.assertEqual(classify_event(boolean_event, content).status, GateStatus.EVIDENCE_ONLY)


if __name__ == "__main__":
    unittest.main()
