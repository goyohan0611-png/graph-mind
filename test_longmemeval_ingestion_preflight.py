import unittest

from longmemeval_ingestion_preflight import cost, serialize_session


class LongMemEvalIngestionPreflightTests(unittest.TestCase):
    def test_session_serialization_preserves_provenance(self):
        value = serialize_session("2024/01/01 (Mon) 12:00", "s1", [
            {"role": "user", "content": "I moved."},
            {"role": "assistant", "content": "Understood."},
        ])
        self.assertIn("SESSION_ID: s1", value)
        self.assertIn("TURN_000 [user]: I moved.", value)

    def test_batch_cost_is_half_standard(self):
        self.assertAlmostEqual(cost(1_000_000, 1_000_000, True),
                               cost(1_000_000, 1_000_000, False) / 2)


if __name__ == "__main__":
    unittest.main()
