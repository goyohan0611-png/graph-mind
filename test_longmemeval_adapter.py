import unittest

from longmemeval_adapter import (evidence_session_ids_from_turns, flatten_turns,
                                  select_development_pilot, validate_instance)


def instance(question_id, question_type="multi-session"):
    return {
        "question_id": question_id, "question_type": question_type,
        "question": "What changed?", "question_date": "2024/01/02 (Tue) 12:00",
        "answer": "A", "answer_session_ids": ["s1"],
        "haystack_dates": ["2024/01/01 (Mon) 12:00"],
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [[{"role": "user", "content": "A", "has_answer": True},
                               {"role": "assistant", "content": "OK"}]],
    }


class LongMemEvalAdapterTests(unittest.TestCase):
    def test_validation_and_flattening(self):
        row = instance("q1")
        self.assertEqual(validate_instance(row), [])
        turns = flatten_turns(row)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["turn_id"], "s1:turn-000")
        self.assertEqual(evidence_session_ids_from_turns(row), {"s1"})

    def test_bad_parallel_lengths_are_reported(self):
        row = instance("q1")
        row["haystack_dates"] = []
        self.assertTrue(validate_instance(row))

    def test_pilot_selection_is_stable_and_includes_abstention(self):
        types = ("single-session-user", "single-session-assistant",
                 "single-session-preference", "temporal-reasoning",
                 "knowledge-update", "multi-session")
        rows = [instance("keep_abs", "multi-session")]
        for kind in types:
            rows.extend(instance(f"{kind}-{number}", kind) for number in range(3))
        first = select_development_pilot(rows, per_type=2)
        second = select_development_pilot(reversed(rows), per_type=2)
        self.assertEqual(first, second)
        self.assertIn("keep_abs", first)
        self.assertEqual(len(first), 13)


if __name__ == "__main__":
    unittest.main()
