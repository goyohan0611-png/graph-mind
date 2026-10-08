import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import unittest

from executable_memory import TypedMemoryIndex
from temporal_state import execute_state_query, StateScopeCertificate


def query(mode="LATEST_OBSERVATION", **changes):
    return dict(dict(mode=mode, memory_id="m", entity_id="shoe", relation="LOCATED_AT",
                     source_scope="USER_MEMORY", clock="SOURCE_LOCAL", as_of="2023-06-01T12:00:00"), **changes)


def observation(eid="o1", location="under bed", when="2023-05-25T12:00:00", **changes):
    return dict(dict(evidence_id=eid, memory_id="m", relation="LOCATED_AT", object_text=location,
                     source_roles=["user"], fact_mode="ASSERTED", state_kind="OBSERVATION",
                     observed_at=when, recorded_at=when, observation_time_verified=True,
                     participants=[{"role": "SUBJECT", "entity_id": "shoe", "source_role": "user"}]), **changes)


def change(eid="c1", location="rack", start="2023-05-28T12:00:00", **changes):
    return observation(eid, location, start, **dict(dict(state_kind="STATE_CHANGE", state_time_verified=True,
                                                        state_valid_from=start), **changes))


def certificate(**changes):
    fields = dict(memory_id="m", entity_id="shoe", relation="LOCATED_AT", source_scope="USER_MEMORY",
                  knowledge_through="2023-06-01T12:00:00", history_complete=True, carry_forward=True)
    return StateScopeCertificate(**dict(fields, **changes))


class TemporalTests(unittest.TestCase):
    def run_query(self, events, q=None, cert=None):
        return execute_state_query(q or query(), TypedMemoryIndex(events), cert)["result"]

    def test_latest_observation_is_not_current_state(self):
        event = observation()
        result = self.run_query([event], query(max_observation_age_seconds=86400))
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(result["observation"]["freshness"], "STALE")
        self.assertFalse(result["continued_validity_established"])
        current = self.run_query([event], query("CURRENT_STATE"))
        self.assertEqual(current["reason"], "STATE_SCOPE_NOT_CERTIFIED")
        self.assertEqual(current["last_observation"]["value"], "under bed")

    def test_fresh_observation_and_complete_history_are_not_valid_interval(self):
        event = observation(when="2023-06-01T11:59:00")
        self.assertEqual(self.run_query([event], query(max_observation_age_seconds=120))["observation"]["freshness"], "FRESH")
        current = self.run_query([event], query("CURRENT_STATE"), certificate())
        self.assertEqual(current["reason"], "NO_CERTIFIED_VALID_STATE")

    def test_latest_ignores_future_plan_and_orders_observations(self):
        events = [observation(), observation("o2", "rack", "2023-05-30T12:00:00"),
                  observation("plan", "garage", fact_mode="PLANNED", recorded_at=None)]
        self.assertEqual(self.run_query(events)["value"], "rack")

    def test_conflicting_simultaneous_observations_abstain(self):
        result = self.run_query([observation(), observation("o2", "rack")])
        self.assertEqual(result["reason"], "CONFLICTING_OBSERVATIONS")
        self.assertEqual(result["evidence_event_ids"], ["o1", "o2"])

    def test_same_value_simultaneous_observations_preserve_both_evidence(self):
        result = self.run_query([observation(), observation("o2")])
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(result["evidence_event_ids"], ["o1", "o2"])

    def test_undated_or_unverified_observation_cannot_hide_behind_old_record(self):
        for event in [observation("o2", observed_at=None), observation("o2", observation_time_verified=False)]:
            self.assertEqual(self.run_query([observation(), event])["status"], "UNKNOWN")

    def test_source_local_and_utc_are_explicit_clock_domains(self):
        event = observation(when="2023-05-25T21:00:00+09:00")
        q = query(clock="UTC", as_of="2023-06-01T12:00:00Z")
        self.assertEqual(self.run_query([event], q)["observation"]["observed_at"], "2023-05-25T12:00:00+00:00")
        self.assertEqual(self.run_query([event])["reason"], "RECORDING_CLOCK_DOMAIN_MISMATCH")

    def test_late_arrival_obeys_knowledge_cutoff(self):
        event = change(recorded_at="2023-06-02T12:00:00")
        self.assertEqual(self.run_query([event], query("STATE_AS_OF"), certificate())["reason"], "NO_CERTIFIED_VALID_STATE")
        q = query("STATE_AS_OF", knowledge_cutoff="2023-06-03T12:00:00")
        cert = certificate(knowledge_through="2023-06-03T12:00:00")
        self.assertEqual(self.run_query([event], q, cert)["value"], "rack")

    def test_verified_changes_require_explicit_carry_policy(self):
        self.assertEqual(self.run_query([change()], query("CURRENT_STATE"), certificate(carry_forward=False))["reason"],
                         "STATE_PERSISTENCE_NOT_CERTIFIED")
        result = self.run_query([change()], query("CURRENT_STATE"), certificate())
        self.assertEqual(result["status"], "ANSWER")
        self.assertEqual(result["claim_scope"], "CERTIFIED_MEMORY_MODEL")

    def test_state_change_chain_does_not_resurrect_expired_latest_state(self):
        events = [change("old", "under bed", "2023-05-25T12:00:00"),
                  change("new", "rack", state_valid_to="2023-05-31T12:00:00")]
        self.assertEqual(self.run_query(events, query("CURRENT_STATE"), certificate())["reason"], "NO_CERTIFIED_VALID_STATE")

    def test_valid_interval_is_start_inclusive_end_exclusive(self):
        event = change(state_kind="VALID_INTERVAL", state_valid_to="2023-06-01T12:00:00")
        self.assertEqual(self.run_query([event], query("STATE_AS_OF", as_of="2023-05-28T12:00:00"), certificate())["value"], "rack")
        self.assertEqual(self.run_query([event], query("STATE_AS_OF"), certificate())["reason"], "NO_CERTIFIED_VALID_STATE")

    def test_overlapping_conflicting_valid_intervals_abstain(self):
        events = [change("a", "rack", state_kind="VALID_INTERVAL"), change("b", "garage", state_kind="VALID_INTERVAL")]
        self.assertEqual(self.run_query(events, query("CURRENT_STATE"), certificate())["reason"], "CONFLICTING_VALID_STATES")

    def test_observation_can_contradict_certified_state(self):
        events = [change(), observation("o2", "garage", "2023-05-30T12:00:00")]
        self.assertEqual(self.run_query(events, query("CURRENT_STATE"), certificate())["reason"], "OBSERVATION_CONTRADICTS_VALID_STATE")

    def test_wrong_incomplete_or_stale_certificate_cannot_authorize_answer(self):
        for cert in [certificate(entity_id="other"), certificate(history_complete=False),
                     certificate(knowledge_through="2023-05-31T12:00:00")]:
            self.assertEqual(self.run_query([change()], query("CURRENT_STATE"), cert)["status"], "UNKNOWN")
        q = query("CURRENT_STATE", history_complete=True, carry_forward=True)
        self.assertEqual(self.run_query([change()], q)["reason"], "STATE_SCOPE_NOT_CERTIFIED")

    def test_scope_and_id_postings_exclude_other_users_and_assistant(self):
        foreign = observation("foreign", "garage", memory_id="other")
        assistant = observation("assistant", "closet", source_roles=["assistant"],
                                participants=[{"role": "SUBJECT", "entity_id": "shoe", "source_role": "assistant"}])
        execution = execute_state_query(query(), TypedMemoryIndex([observation(), foreign, assistant]))
        self.assertEqual(execution["result"]["value"], "under bed")
        self.assertEqual(execution["audit"]["foreign_memory_excluded"], 1)

    def test_future_observation_and_state_change_do_not_become_completed_fact(self):
        bad = observation(recorded_at="2023-05-24T12:00:00")
        self.assertEqual(self.run_query([bad])["reason"], "OBSERVATION_AFTER_RECORDING")
        bad = change(recorded_at="2023-05-24T12:00:00")
        self.assertEqual(self.run_query([bad], query("CURRENT_STATE"), certificate())["reason"], "STATE_CHANGE_AFTER_RECORDING")

    def test_current_cannot_use_old_knowledge_and_invalid_age_is_rejected(self):
        q = query("CURRENT_STATE", knowledge_cutoff="2023-05-31T12:00:00")
        self.assertEqual(self.run_query([change()], q, certificate())["reason"], "CURRENT_KNOWLEDGE_CUTOFF_BEFORE_AS_OF")
        for age in [-1, True, float("nan")]:
            self.assertEqual(self.run_query([observation()], query(max_observation_age_seconds=age))["reason"], "INVALID_OBSERVATION_AGE_POLICY")

    def test_location_filter_cannot_hide_conflicting_location(self):
        events = [observation(), observation("o2", "rack")]
        q = query(entity_terms=["under bed"], event_ids=["o1"])
        self.assertEqual(self.run_query(events, q)["reason"], "CONFLICTING_OBSERVATIONS")

    def test_state_query_uses_entity_postings_with_ten_thousand_distractors(self):
        unrelated = [observation("d" + str(i), participants=[
            {"role": "SUBJECT", "entity_id": "other-" + str(i), "source_role": "user"}]) for i in range(10000)]
        execution = execute_state_query(query(), TypedMemoryIndex([observation()] + unrelated))
        self.assertEqual(execution["result"]["status"], "OBSERVED")
        self.assertEqual(execution["audit"]["relation_candidates"], 10001)
        self.assertEqual(execution["audit"]["examined_events"], 1)
        self.assertEqual(execution["audit"]["candidate_source"], "PARTICIPANT_ID_POSTINGS")


if __name__ == "__main__":
    unittest.main()
