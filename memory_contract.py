"""Versioned shared schemas and source-anchored participant normalization."""
from copy import deepcopy
import hashlib
from decimal import Decimal
import re

from executable_plan_v2_diagnostic import SCHEMA as OLD_PLAN_SCHEMA
from longmemeval_ingestion_v7 import SCHEMA as OLD_EVENT_SCHEMA, turn_texts, sanitize_enrichment
from ingestion_gate import classify_event, GateStatus, parse_turn_roles
from memory_vocabulary import CATEGORIES, ENTITY_TYPES, PARTICIPANT_ROLES, FACT_MODES, SEMANTICS
from executable_memory import normalize

CONTRACT_VERSION = "1"
NULL_STRING = {"type": ["string", "null"]}


def strict_object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def bounded_array(items, maximum=4, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


PARTICIPANT = strict_object({
    "role": {"type": "string", "enum": list(PARTICIPANT_ROLES)},
    "name": {"type": "string"},
    "entity_type": {"type": "string", "enum": [t for t in ENTITY_TYPES if t != "NONE"]},
    "source_turn_id": {"type": "string"}, "support_quote": {"type": "string"}})
PARTICIPANT_FILTER = strict_object({
    "role": {"type": "string", "enum": list(PARTICIPANT_ROLES)},
    "names": bounded_array({"type": "string"}, minimum=1)})

EVENT_SCHEMA = deepcopy(OLD_EVENT_SCHEMA)
EVENT_SCHEMA["properties"]["events"]["maxItems"] = 32
event = EVENT_SCHEMA["properties"]["events"]["items"]
event["properties"]["participants"] = bounded_array(PARTICIPANT, maximum=6)
event["properties"]["fact_mode"] = {"type": "string", "enum": list(FACT_MODES)}
event["properties"]["category_tags"] = bounded_array({"type": "string", "enum": list(CATEGORIES)})
event["properties"]["object_aliases"] = bounded_array({"type": "string"})
event["required"] += ["participants", "fact_mode"]

PLAN_SCHEMA = deepcopy(OLD_PLAN_SCHEMA)
PLAN_SCHEMA["properties"]["result_operand"] = NULL_STRING
PLAN_SCHEMA["required"].append("result_operand")
PLAN_SCHEMA["properties"]["operands"].update(minItems=1, maxItems=6)
operand = PLAN_SCHEMA["properties"]["operands"]["items"]
operand["properties"]["participant_terms"] = bounded_array(PARTICIPANT_FILTER)
operand["properties"]["fact_modes"] = bounded_array(
    {"type": "string", "enum": list(FACT_MODES)}, maximum=2, minimum=1)
operand["required"] += ["participant_terms", "fact_modes"]
for key in ("relations", "relation_details", "entity_terms", "category_tags"):
    operand["properties"][key]["maxItems"] = 4
operand["properties"]["relations"]["minItems"] = 1
operand["properties"]["category_tags"]["items"] = {"type": "string", "enum": list(CATEGORIES)}
PLAN_SCHEMA["properties"]["abstain_if"]["maxItems"] = 4

INGESTION_PROMPT = """Extract durable memory events from one chat session. No later question or
answer is available. Keep directly supported facts, typed numeric values and units, actual states,
plans, and provenance. Omit generic advice, puzzles, transient small talk and boilerplate.
Role labels are authoritative. Every event must cite supporting TURN_NNN IDs.
Attach explicit participants with a role and entity type, including the beneficiary of a cost and
the item whose location is described. Each participant needs an exact source quote containing its
name, from a cited turn, that supports its connection to THIS event. If a pronoun is used, quote a
large enough directly supporting turn containing the explicit name; do not guess links from mere
co-occurrence elsewhere in the session. Emit no participant when support is absent. Do not invent
date/year, completed state, alias, entity link, or unit. Do not repeat the same fact just because it
was restated. Preserve raw relation wording and time wording. Return only JSON.
""" + SEMANTICS
PLAN_PROMPT = """Compile only the question and question_date into the shared executable contract.
Memory, reference answer and evidence are unavailable. Do not answer. Use explicit typed operands,
absolute ISO ranges or event-relative time_relation/time_reference_operand. For LOOKUP/LATEST/EARLIEST
set result_operand to the one operand to return; auxiliary operands may be present. For COUNT name
aggregate_operand, require complete scope, and use an explicit distinct key. For other operators set
aggregate_operand null. Binary operators use left/right operand order; DATE_DIFF uses start/end.
SUM of two particular stored fees should use two UNIQUE value operands; SUM over all occurrences
needs ALL and complete scope. Missing required operands mean UNKNOWN regardless of required flag.
I/my questions use USER_MEMORY, which is provenance scope, not the subject name. Plans must not
presuppose an answer exists. Set unused filters to []; use participant_terms for named participants.
Current location means ASSERTED states only. A location LOOKUP can return an actual stored state;
use LATEST only when selecting among potentially changing states and preserve unknown time semantics.
No arbitrary abstention policies: leave abstain_if empty; missing/ambiguous values are handled by
the executor. Keep the plan compact and return only JSON.
""" + SEMANTICS


def surface_entity_id(memory_id, entity_type, name):
    """Legacy v1 ID for frozen replays; new intake uses registry projection."""
    identity = f"{memory_id}::{entity_type}::{normalize(name)}"
    return "entity_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def grounded_money_unit(event, turns):
    """Recover a literal currency symbol next to the SAME scalar in cited turns.

    '$' stays '$'; it is not converted into an assumed USD currency. This is
    formatting recovery, not a verifier that the scalar belongs to the event.
    """
    if event.get("value_type") != "MONEY" or event.get("unit") or event.get("numeric_value") is None:
        return None, []
    wanted = Decimal(str(event["numeric_value"]))
    matches = []
    pattern = r"(?<![\w$])([$£€])\s*(\d[\d,]*(?:\.\d+)?)(?!\d|\.\d)"
    for tid in event.get("source_turn_ids", []):
        for match in re.finditer(pattern, turns.get(tid, "")):
            if Decimal(match.group(2).replace(",", "")) == wanted:
                matches.append({"source_turn_id": tid, "quote": match.group(0), "unit": match.group(1)})
    units = {match["unit"] for match in matches}
    return (next(iter(units)), matches) if len(units) == 1 else (None, [])


def normalize_event(raw, content, memory_id, evidence_id, *, registry=None, identity_keys=None):
    event, removed_aliases, removed_tags = sanitize_enrichment(raw, content)
    turns, roles = turn_texts(content), parse_turn_roles(content)
    recovered_unit, unit_sources = grounded_money_unit(event, turns)
    if recovered_unit:
        event["unit"] = recovered_unit
        event["unit_source_anchors"] = unit_sources
    decision = classify_event(event, content)
    audit = {"gate": decision.status.value, "removed_aliases": removed_aliases,
             "removed_categories": removed_tags, "rejected_participants": [],
             "gate_reasons": list(decision.reasons), "unit_recovery": unit_sources}
    if decision.status != GateStatus.PERSIST:
        return None, audit
    participants = []
    for participant in raw.get("participants", []):
        tid, quote = participant["source_turn_id"], participant["support_quote"]
        name = normalize(participant["name"])
        valid = (tid in event["source_turn_ids"] and bool(quote.strip())
                 and quote in turns.get(tid, "") and bool(name)
                 and " " + name + " " in " " + normalize(quote) + " "
                 and participant["role"] in PARTICIPANT_ROLES
                 and participant["entity_type"] in ENTITY_TYPES
                 and participant["entity_type"] != "NONE")
        if not valid:
            audit["rejected_participants"].append(participant)
            continue
        participants.append(dict(participant, source_role=roles[tid], entity_id=surface_entity_id(
            memory_id, participant["entity_type"], participant["name"])))
    event.update(contract_version=CONTRACT_VERSION, participants=participants,
                 evidence_id=evidence_id, source_roles=sorted({roles[t] for t in event["source_turn_ids"]}))
    name = event.get("object_canonical_name") or event.get("object_text")
    event["canonical_entity_id"] = (surface_entity_id(memory_id, event["object_type"], name)
                                    if name and event["object_type"] != "NONE" else None)
    # No occurrence identity or semantic scope certificate can be manufactured from a fact ID.
    event["occurrence_id"] = None
    if registry is not None:
        event = registry.project_event(memory_id, event, identity_keys)
        audit["identity_version"] = "2"
    elif identity_keys is not None:
        raise ValueError("IDENTITY_LINK_REQUIRES_REGISTRY")
    audit["participant_grounding_limit"] = "Quote/name anchoring only; semantic entailment not independently verified."
    return event, audit
