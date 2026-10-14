import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest

from precondition_verifier import normalize_phrase, verify_preconditions


class PreconditionVerifierTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_phrase("Software-Engineer Manager!"),
                         "software engineer manager")

    def test_related_role_does_not_satisfy_exact_title(self):
        plan = {"relation_hints": ["ROLE_ASSIGNMENT"],
                "entities": ["Software Engineer Manager", "you", "engineers"]}
        events = [{"session_id": "s1", "event_local_id": "e1", "subject": "user",
                   "relation": "HAS_ATTRIBUTE", "relation_detail": None,
                   "object_text": "Senior Software Engineer"}]
        result = verify_preconditions(plan, events)
        self.assertTrue(result["must_abstain"])
        self.assertEqual(result["missing_entities"], ["Software Engineer Manager"])

    def test_exact_role_passes(self):
        plan = {"relation_hints": ["ROLE_ASSIGNMENT"],
                "entities": ["Software Engineer Manager", "you"]}
        events = [{"session_id": "s1", "event_local_id": "e1", "subject": "user",
                   "relation": "HAS_ATTRIBUTE", "relation_detail": "current role",
                   "object_text": "Software Engineer Manager"}]
        result = verify_preconditions(plan, events)
        self.assertNotIn("must_abstain", result)
        self.assertEqual(result["verified_exact_entities"][0]["entity"],
                         "Software Engineer Manager")

    def test_noncritical_plan_is_unchanged(self):
        plan = {"relation_hints": ["LOCATION"], "entities": ["Central Park"]}
        self.assertEqual(verify_preconditions(plan, []), {})


if __name__ == "__main__":
    unittest.main()
