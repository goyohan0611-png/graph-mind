"""Conservative deterministic execution of an external LLM's typed plan.

Lexical alternatives within a filter are OR; different filters are AND. No
synonyms, dates, occurrence IDs, or completeness certificates are invented.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import re

import memory_algebra as algebra
from memory_normalization import normalize, normalize_unit


def participant_in_scope(participant, event, scope):
    role = participant.get("source_role")
    if role is None and len(event.get("source_roles", [])) == 1:
        role = event["source_roles"][0]
    return role in {"user", "assistant"} and role in event.get("source_roles", []) and (scope == "BOTH" or
        (scope == "USER_MEMORY" and role == "user") or
        (scope == "ASSISTANT_HISTORY" and role == "assistant"))


def iso(value: object) -> datetime | None:
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", str(value)):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class TypedMemoryIndex:
    def __init__(self, events: list[dict]):
        self.events = events
        self.by_relation: dict[str, list[dict]] = {}
        self.by_participant: dict[tuple[str, str], set[int]] = {}
        self.by_event_id: dict[str, int] = {}
        for position, event in enumerate(events):
            self.by_relation.setdefault(event["relation"], []).append(event)
            if event.get("evidence_id"):
                if event["evidence_id"] in self.by_event_id:
                    raise ValueError("DUPLICATE_EVIDENCE_ID")
                self.by_event_id[event["evidence_id"]] = position
            for participant in event.get("participants", []):
                if participant.get("entity_id"):
                    self.by_participant.setdefault((participant["role"], participant["entity_id"]), set()).add(position)

    def candidates(self, operand: dict) -> tuple[list[dict], dict]:
        allowed = set(operand["relations"])
        total_relation_rows = sum(len(self.by_relation.get(relation, [])) for relation in allowed)
        if operand.get("participant_bindings") or operand.get("event_ids"):
            positions = None
            for binding in operand.get("participant_bindings", []):
                hits = set().union(*(self.by_participant.get((binding["role"], eid), set())
                                     for eid in binding["entity_ids"]))
                positions = hits if positions is None else positions & hits
            if operand.get("event_ids"):
                hits = {self.by_event_id[eid] for eid in operand["event_ids"] if eid in self.by_event_id}
                positions = hits if positions is None else positions & hits
            relation_rows = [self.events[position] for position in sorted(positions or [])
                             if self.events[position]["relation"] in allowed]
            candidate_source = "EVENT_ID_POSTINGS" if operand.get("event_ids") else "PARTICIPANT_ID_POSTINGS"
        else:
            relation_rows = [event for relation in dict.fromkeys(operand["relations"])
                             for event in self.by_relation.get(relation, [])]
            candidate_source = "RELATION_SCAN"
        rows = []
        rejected = {"source": 0, "entity": 0, "detail": 0, "category": 0}
        for event in relation_rows:
            roles = set(event.get("source_roles") or [])
            scope = operand["source_scope"]
            if (not roles or (scope == "USER_MEMORY" and "user" not in roles)
                    or (scope == "ASSISTANT_HISTORY" and "assistant" not in roles)):
                rejected["source"] += 1
                continue
            if operand.get("fact_modes") and event.get("fact_mode") not in operand["fact_modes"]:
                rejected.setdefault("fact_mode", 0)
                rejected["fact_mode"] += 1
                continue
            if operand.get("participant_terms") and not all(any(
                    participant.get("role") == constraint["role"]
                    and participant_in_scope(participant, event, scope)
                    and normalize(participant.get("name")) in {
                        normalize(name) for name in constraint["names"]}
                    for participant in event.get("participants", []))
                    for constraint in operand["participant_terms"]):
                rejected.setdefault("participant", 0)
                rejected["participant"] += 1
                continue
            if operand.get("participant_bindings") and not all(any(
                    participant.get("role") == constraint["role"]
                    and participant_in_scope(participant, event, scope)
                    and participant.get("entity_id") in constraint["entity_ids"]
                    for participant in event.get("participants", []))
                    for constraint in operand["participant_bindings"]):
                rejected.setdefault("participant_id", 0)
                rejected["participant_id"] += 1
                continue
            text = normalize(" ".join(str(event.get(key) or "") for key in (
                "subject", "object_text", "object_canonical_name", "relation_detail"))
                + " " + " ".join(event.get("object_aliases") or []))
            tags = {normalize(tag) for tag in event.get("category_tags") or []}
            groups = (("entity", operand.get("entity_terms", []), text),
                      ("detail", operand.get("relation_details", []), text))
            failed = False
            for label, terms, target in groups:
                if terms and not any(" " + normalize(term) + " " in " " + target + " "
                                     for term in terms):
                    rejected[label] += 1
                    failed = True
                    break
            if failed:
                continue
            if operand.get("category_tags") and not any(
                    normalize(tag) in tags for tag in operand["category_tags"]):
                rejected["category"] += 1
                continue
            rows.append(event)
        return rows, {"relation_candidates": total_relation_rows,
                      "examined_events": len(relation_rows), "candidate_source": candidate_source,
                      "lexical_matches": len(rows), "rejected": rejected}


def _when(event: dict) -> datetime | None:
    # Session timestamps date a statement, not the described event.
    if event.get("time_granularity") not in {None, "DAY", "SECOND", "MINUTE", "INSTANT"}:
        return None
    return iso(event.get("time_start")) or iso(event.get("date_value"))


def _atom(event: dict, operand: dict) -> algebra.MemoryAtom | None:
    field = operand["value_field"]
    unit = event.get("unit")
    if operand.get("unit") and normalize_unit(unit) != normalize_unit(operand["unit"]):
        return None
    kind = operand["value_type"]
    if field == "NUMERIC_VALUE":
        value = event.get("numeric_value")
        if kind != event.get("value_type") or not isinstance(value, (int, float)):
            return None
    elif field in {"DATE_VALUE", "SESSION_DATE"}:
        when = _when(event) if field == "DATE_VALUE" else iso(event.get("session_timestamp"))
        if when is None or kind != "DATE":
            return None
        value = when.isoformat()
    elif field == "EVENT_COUNT":
        value, kind, unit = event["evidence_id"], "TEXT", None
    else:
        value = event.get("object_text")
        if kind not in {"TEXT", "ENTITY"} or not value:
            return None
    try:
        return algebra.MemoryAtom(kind, value, (event["evidence_id"],), _when(event), unit)
    except ValueError:
        return None


def execute_plan(plan: dict, index: TypedMemoryIndex,
                 scope_certificates: set[str] | None = None) -> dict:
    """A certificate names an operand whose extraction/filter scope is complete.

    Ingesting every session alone does not provide this certificate. A COUNT
    without one returns UNKNOWN even if the current typed index has matches.
    """
    certificates = scope_certificates or set()
    operands = plan.get("operands", [])
    names = [item["name"] for item in operands]
    trace: dict[str, dict] = {}
    selected: dict[str, list[dict]] = {}
    atoms: dict[str, list[algebra.MemoryAtom]] = {}

    def finish(result: algebra.AlgebraResult) -> dict:
        return {"result": asdict(result), "operands": trace}

    def fail(reason: str) -> dict:
        return finish(algebra.unknown(reason))

    if not operands or len(set(names)) != len(names):
        return fail("INVALID_OPERAND_NAMES")
    for operand in operands:
        if "event_ids" in operand and not operand["event_ids"]:
            return fail("EMPTY_EVENT_ID_BINDING")
        if operand.get("source_scope") not in {"USER_MEMORY", "ASSISTANT_HISTORY", "BOTH"}:
            return fail("INVALID_SOURCE_SCOPE")
        if operand.get("selector") not in {"ALL", "UNIQUE", "LATEST", "EARLIEST"}:
            return fail("INVALID_SELECTOR")
        if operand.get("value_field") not in {
                "OBJECT_TEXT", "NUMERIC_VALUE", "DATE_VALUE", "SESSION_DATE", "EVENT_COUNT"}:
            return fail("INVALID_VALUE_FIELD")
    unary = plan["operator"] in {"LATEST", "EARLIEST", "LOOKUP"}
    result_name = plan.get("result_operand") or (names[0] if unary and len(names) == 1 else None)
    if unary:
        if result_name not in names:
            return fail("RESULT_OPERAND_UNSPECIFIED")
        active = {result_name} | {item["name"] for item in operands if item.get("required")}
        while True:
            closure = active | {item["time_reference_operand"] for item in operands
                                if item["name"] in active and item.get("time_reference_operand")}
            if closure == active:
                break
            active = closure
        operands = [item for item in operands if item["name"] in active]
        names = [item["name"] for item in operands]
    pending = list(operands)
    while pending:
        progressed = False
        for operand in list(pending):
            name = operand["name"]
            reference = operand.get("time_reference_operand")
            if reference and reference not in names:
                return fail(name + ":UNKNOWN_TIME_REFERENCE")
            if reference and reference not in selected:
                continue
            events, audit = index.candidates(operand)
            trace[name] = audit
            time_relation = operand.get("time_relation", "ANY")
            boundary = None
            if time_relation != "ANY":
                reference_rows = selected.get(reference, [])
                if len(reference_rows) != 1 or _when(reference_rows[0]) is None:
                    return fail(name + ":TIME_REFERENCE_MISSING_OR_NON_UNIQUE")
                boundary = _when(reference_rows[0])
            start = iso(operand.get("time_start"))
            end = iso(operand.get("time_end_exclusive"))
            if ((operand.get("time_start") and start is None)
                    or (operand.get("time_end_exclusive") and end is None)):
                return fail(name + ":INVALID_TIME_BOUND")
            if start and end and start >= end:
                return fail(name + ":INVALID_TIME_RANGE")
            if start or end or boundary:
                if any(_when(event) is None for event in events):
                    return fail(name + ":MATCHING_EVENT_TIME_UNRESOLVED")
                events = [event for event in events if
                          (start is None or _when(event) >= start)
                          and (end is None or _when(event) < end)]
                if boundary:
                    predicates = {"BEFORE": lambda value: value < boundary,
                                  "AFTER": lambda value: value > boundary,
                                  "ON_OR_BEFORE": lambda value: value <= boundary,
                                  "ON_OR_AFTER": lambda value: value >= boundary}
                    if time_relation not in predicates:
                        return fail(name + ":UNSUPPORTED_TIME_RELATION")
                    events = [event for event in events if predicates[time_relation](_when(event))]
            selector = operand.get("selector", "ALL")
            if events and selector in {"LATEST", "EARLIEST"}:
                if any(_when(event) is None for event in events):
                    return fail(name + ":SELECTION_TIME_UNRESOLVED")
                when = (max if selector == "LATEST" else min)(_when(event) for event in events)
                events = [event for event in events if _when(event) == when]
                if len(events) != 1:
                    return fail(name + ":SELECTION_TIE")
            if selector == "UNIQUE" and len(events) != 1:
                return fail(name + ":MISSING_OR_NON_UNIQUE")
            selected[name] = events
            atoms[name] = [_atom(event, operand) for event in events]
            audit["selected_events"] = len(events)
            audit["value_failures"] = sum(atom is None for atom in atoms[name])
            if audit["value_failures"]:
                return fail(name + ":VALUE_OR_UNIT_UNRESOLVED")
            pending.remove(operand)
            progressed = True
        if not progressed:
            return fail("CYCLIC_TIME_REFERENCE")
    operator = plan["operator"]
    if operator == "COUNT":
        name = plan.get("aggregate_operand")
        if name not in names:
            return fail("COUNT_OPERAND_UNSPECIFIED")
        if name not in certificates:
            return fail("COUNT_SCOPE_NOT_CERTIFIED")
        operand = operands[names.index(name)]
        key_field = {"CANONICAL_ENTITY_ID": "canonical_entity_id",
                     "OCCURRENCE_ID": "occurrence_id", "OBJECT_TEXT": "object_text",
                     "EVENT_ID": "evidence_id"}.get(operand.get("distinct_by"))
        if key_field is None:
            return fail("COUNT_DISTINCT_KEY_UNSPECIFIED")
        events = selected[name]
        if any(not event.get(key_field) for event in events):
            return fail("COUNT_DISTINCT_KEY_MISSING")
        if not events and plan.get("empty_result") != "ZERO_IF_COMPLETE":
            return fail("EMPTY_RESULT_UNKNOWN")
        unique = {}
        for event in events:
            key = normalize(event[key_field]) if key_field == "object_text" else event[key_field]
            unique.setdefault(key, event)
        return finish(algebra.AlgebraResult("ANSWER", "NUMBER", len(unique), None,
                      tuple(event["evidence_id"] for event in unique.values()), "CERTIFIED_SCOPE"))
    if plan.get("index_scope_required") and any(name not in certificates for name in names):
        return fail("INDEX_SCOPE_NOT_CERTIFIED")
    if operator == "SUM" and any(operand["selector"] == "ALL"
                                 and operand["name"] not in certificates for operand in operands):
        return fail("SUM_SCOPE_NOT_CERTIFIED")
    if any(not atoms[name] for name in names):
        return fail("MISSING_OPERATOR_VALUE")
    if operator in {"SUBTRACT", "DATE_DIFF", "COMPARE"}:
        if len(names) != 2 or any(len(atoms[name]) != 1 for name in names):
            return fail("BINARY_VALUES_MISSING_OR_NON_UNIQUE")
        left, right = (atoms[name][0] for name in names)
        if operator == "SUBTRACT":
            return finish(algebra.subtract(left, right))
        if operator == "DATE_DIFF":
            return finish(algebra.date_diff(left, right, str(plan.get("result_unit") or "days").lower()))
        return finish(algebra.compare(left, right))
    output_names = [result_name] if unary else names
    values = list({atom.evidence_event_ids: atom for name in output_names for atom in atoms[name]}.values())
    operations = {"SUM": algebra.sum_values, "LATEST": algebra.latest,
                  "EARLIEST": algebra.earliest, "LOOKUP": algebra.lookup}
    if operator not in operations:
        return fail("UNSUPPORTED_OPERATOR")
    return finish(operations[operator](values))
