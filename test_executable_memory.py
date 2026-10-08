import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest

from executable_memory import TypedMemoryIndex, execute_plan


def operand(name="items", **changes):
    result = dict(name=name, source_scope="USER_MEMORY", relations=["VISITED"],
                  relation_details=[], entity_terms=[], category_tags=[],
                  value_field="EVENT_COUNT", value_type="NUMBER", unit=None,
                  selector="ALL", time_start=None, time_end_exclusive=None,
                  time_relation="ANY", time_reference_operand=None,
                  distinct_by="OCCURRENCE_ID", required=True)
    return dict(result, **changes)


def plan(operator="COUNT", operands=None, **changes):
    result = dict(operator=operator, operands=operands or [operand()], aggregate_operand="items",
                  empty_result="ZERO_IF_COMPLETE", index_scope_required=True, result_unit=None)
    return dict(result, **changes)


def event(eid="e1", **changes):
    return dict(dict(relation="VISITED", subject="user", object_text="Art museum",
                     evidence_id=eid, occurrence_id=eid, date_value="2023-01-01",
                     source_roles=["user"]), **changes)


class ExecutionTests(unittest.TestCase):
    def execute(self, query, events, certificates=()):
        return execute_plan(query, TypedMemoryIndex(events), set(certificates))["result"]

    def test_zero_requires_certified_scope(self):
        self.assertEqual(self.execute(plan(), [])["status"], "UNKNOWN")
        self.assertEqual(self.execute(plan(), [], ["items"])["value"], 0)

    def test_positive_count_also_requires_complete_scope(self):
        self.assertEqual(self.execute(plan(), [event()])["status"], "UNKNOWN")

    def test_occurrence_count_deduplicates_repeated_mentions(self):
        events = [event(), event("e2", occurrence_id="e1"), event("e3")]
        self.assertEqual(self.execute(plan(), events, ["items"])["value"], 2)

    def test_missing_occurrence_id_does_not_fall_back_to_event_id(self):
        result = self.execute(plan(), [event(occurrence_id=None)], ["items"])
        self.assertEqual(result["reason"], "COUNT_DISTINCT_KEY_MISSING")

    def test_assistant_fact_does_not_become_user_visit(self):
        result = self.execute(plan(), [event(subject="assistant", source_roles=["assistant"])], ["items"])
        self.assertEqual(result["value"], 0)

    def test_user_memory_scope_is_provenance_not_event_subject(self):
        result = self.execute(plan(), [event(subject="Lola")], ["items"])
        self.assertEqual(result["value"], 1)

    def test_missing_event_date_does_not_use_statement_date(self):
        op = operand(time_start="2023-01-01", time_end_exclusive="2023-02-01")
        result = self.execute(plan(operands=[op]),
                              [event(date_value=None, session_timestamp="2023-01-15")], ["items"])
        self.assertEqual(result["reason"], "items:MATCHING_EVENT_TIME_UNRESOLVED")

    def test_before_uses_explicit_reference_event(self):
        op = operand(time_relation="BEFORE", time_reference_operand="offer")
        ref = operand("offer", relations=["COMPLETED"], selector="UNIQUE",
                      value_field="DATE_VALUE", value_type="DATE")
        events = [event(), event("e2", date_value="2023-03-01"),
                  event("e3", relation="COMPLETED", date_value="2023-02-01")]
        result = self.execute(plan(operands=[op, ref]), events, ["items"])
        self.assertEqual(result["value"], 1)
        self.assertEqual(result["evidence_event_ids"], ("e1",))

    def test_sum_uses_explicit_values_and_rejects_unit_mismatch(self):
        ops = [operand("vet", relations=["HAS_COST"], entity_terms=["vet"],
                       value_field="NUMERIC_VALUE", value_type="MONEY", selector="UNIQUE"),
               operand("med", relations=["HAS_COST"], entity_terms=["med"],
                       value_field="NUMERIC_VALUE", value_type="MONEY", selector="UNIQUE")]
        query = plan("SUM", ops, index_scope_required=False, aggregate_operand=None)
        events = [event(relation="HAS_COST", object_text="vet", value_type="MONEY",
                        numeric_value=50, unit="USD"),
                  event("e2", relation="HAS_COST", object_text="med", value_type="MONEY",
                        numeric_value=20, unit="USD")]
        self.assertEqual(self.execute(query, events)["value"], 70)
        events[1]["unit"] = "EUR"
        self.assertEqual(self.execute(query, events)["reason"], "INCOMPATIBLE_TYPES_OR_UNITS")

    def test_reversed_time_range_cannot_prove_zero(self):
        op = operand(time_start="2023-02-01", time_end_exclusive="2023-01-01")
        result = self.execute(plan(operands=[op]), [], ["items"])
        self.assertEqual(result["reason"], "items:INVALID_TIME_RANGE")

    def test_sum_of_all_matches_requires_scope_certificate(self):
        op = operand(relations=["HAS_COST"], value_field="NUMERIC_VALUE", value_type="MONEY")
        query = plan("SUM", [op], index_scope_required=False, aggregate_operand=None)
        events = [event(relation="HAS_COST", value_type="MONEY", numeric_value=20, unit="USD")]
        self.assertEqual(self.execute(query, events)["reason"], "SUM_SCOPE_NOT_CERTIFIED")

    def test_latest_does_not_choose_arbitrarily_among_ties(self):
        op = operand(selector="LATEST", value_field="OBJECT_TEXT", value_type="TEXT")
        query = plan("LATEST", [op], index_scope_required=False, aggregate_operand=None)
        result = self.execute(query, [event(), event("e2", object_text="Science museum")])
        self.assertEqual(result["reason"], "items:SELECTION_TIE")


if __name__ == "__main__":
    unittest.main()
