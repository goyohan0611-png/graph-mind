"""Deterministic precondition checks before Graph-MIND answer execution."""
from __future__ import annotations

import re


GENERIC_ENTITIES = {
    "i", "me", "my", "mine", "you", "your", "yours", "user",
    "person", "people", "team", "engineer", "engineers", "employee", "employees",
}

EXACT_ENTITY_RELATIONS = {
    "ROLE_ASSIGNMENT": {
        "HAS_ATTRIBUTE", "HAS_ROLE", "JOB_TITLE", "OCCUPATION", "ROLE_ASSIGNMENT", "WORKS_AT",
    },
}


def normalize_phrase(value: object) -> str:
    """Normalize text for conservative exact phrase comparison."""
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _specific_entities(plan: dict) -> list[str]:
    result = []
    for entity in plan.get("entities", []):
        normalized = normalize_phrase(entity)
        if not normalized or normalized in GENERIC_ENTITIES:
            continue
        result.append(str(entity))
    return result


def _event_text(event: dict) -> str:
    return normalize_phrase(" ".join(str(event.get(field) or "") for field in (
        "subject", "relation", "relation_detail", "object_text")))


def verify_preconditions(plan: dict, events: list[dict]) -> dict:
    """Return an executor hint when a critical exact entity is absent.

    The first policy covers role assignment. Job titles are identity-bearing labels, so a
    related title must not satisfy an exact title in the plan (for example, Senior Software
    Engineer is not Software Engineer Manager).
    """
    relation_hints = set(plan.get("relation_hints", []))
    policies = relation_hints & EXACT_ENTITY_RELATIONS.keys()
    if not policies:
        return {}

    allowed_relations = set().union(*(EXACT_ENTITY_RELATIONS[name] for name in policies))
    evidence = [event for event in events if event.get("relation") in allowed_relations]
    missing = []
    matched = []
    for entity in _specific_entities(plan):
        target = normalize_phrase(entity)
        sources = [f"{event.get('session_id')}::{event.get('event_local_id')}"
                   for event in evidence if target in _event_text(event)]
        if sources:
            matched.append({"entity": entity, "source_events": sources})
        else:
            missing.append(entity)

    if not missing:
        return {"verified_exact_entities": matched} if matched else {}
    return {
        "must_abstain": True,
        "reason": "REQUIRED_EXACT_ENTITY_NOT_FOUND",
        "missing_entities": missing,
        "verified_exact_entities": matched,
        "policy": "Critical identity-bearing entities require exact supporting events.",
    }
