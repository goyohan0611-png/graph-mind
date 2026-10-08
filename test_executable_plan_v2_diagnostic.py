import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest

from executable_plan_v2_diagnostic import structural_audit


def operand(name="items"):
    return {"name": name, "source_scope": "USER_MEMORY", "relations": ["VISITED"],
            "relation_details": [], "entity_terms": [], "category_tags": ["MUSEUM"],
            "value_field": "EVENT_COUNT", "value_type": "NUMBER", "unit": None,
            "selector": "UNIQUE", "time_start": "2022-12-01",
            "time_end_exclusive": "2023-01-01", "time_relation": "ANY",
            "time_reference_operand": None, "distinct_by": "CANONICAL_ENTITY_ID",
            "required": True}


class ExecutablePlanV2Tests(unittest.TestCase):
    def test_count_requires_complete_scope_and_distinct_key(self):
        plan = {"operator": "COUNT", "operands": [operand()], "result_type": "NUMBER",
                "aggregate_operand": "items",
                "result_unit": None, "empty_result": "ZERO_IF_COMPLETE",
                "index_scope_required": True, "abstain_if": [], "confidence": "HIGH"}
        self.assertEqual(structural_audit(plan, "COUNT"), [])
        plan["index_scope_required"] = False
        self.assertIn("COUNT_SCOPE_NOT_REQUIRED", structural_audit(plan, "COUNT"))

    def test_binary_operator_requires_two_operands(self):
        plan = {"operator": "DATE_DIFF", "operands": [operand()], "result_type": "DURATION",
                "aggregate_operand": None,
                "result_unit": "days", "empty_result": "UNKNOWN",
                "index_scope_required": True, "abstain_if": [], "confidence": "HIGH"}
        self.assertIn("BINARY_OPERAND_COUNT", structural_audit(plan, "DATE_DIFF"))

    def test_count_zero_cannot_also_abstain_on_no_match(self):
        plan = {"operator": "COUNT", "operands": [operand()], "aggregate_operand": "items",
                "result_type": "NUMBER", "result_unit": None,
                "empty_result": "ZERO_IF_COMPLETE", "index_scope_required": True,
                "abstain_if": ["NO_MATCHING_MEMORIES"], "confidence": "HIGH"}
        self.assertIn("COUNT_EMPTY_CONTRADICTION", structural_audit(plan, "COUNT"))

    def test_user_question_rejects_both_scope(self):
        item = operand()
        item["source_scope"] = "BOTH"
        plan = {"operator": "COUNT", "operands": [item], "aggregate_operand": "items",
                "result_type": "NUMBER", "result_unit": None,
                "empty_result": "ZERO_IF_COMPLETE", "index_scope_required": True,
                "abstain_if": [], "confidence": "HIGH"}
        self.assertIn("items:SOURCE_SCOPE_LEAK", structural_audit(plan, "COUNT"))


if __name__ == "__main__":
    unittest.main()
