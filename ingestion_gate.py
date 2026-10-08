"""Deterministic safety gate between LLM event extraction and persistent memory."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class GateStatus(str, Enum):
    PERSIST = "PERSIST"
    EVIDENCE_ONLY = "EVIDENCE_ONLY"
    QUARANTINE = "QUARANTINE"


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    reasons: tuple[str, ...]


def parse_turn_roles(content: str) -> dict[str, str]:
    roles: dict[str, str] = {}
    for line in content.splitlines():
        match = re.match(r"^(TURN_\d+) \[(user|assistant)\]:", line, re.IGNORECASE)
        if match:
            roles[match.group(1)] = match.group(2).lower()
    return roles


def role_played_user_turn_ids(content: str) -> set[str]:
    """Detect assistant-channel turns after an explicit request to simulate the user."""
    role_play = False
    result: set[str] = set()
    for line in content.splitlines():
        match = re.match(r"^(TURN_\d+) \[(user|assistant)\]:(.*)$", line, re.IGNORECASE)
        if not match:
            continue
        turn_id, role, text = match.group(1), match.group(2).lower(), match.group(3).lower()
        if role == "user" and "respond as the user" in text:
            role_play = True
            continue
        if role_play and role == "assistant":
            result.add(turn_id)
    return result


def role_play_block_turn_ids(content: str) -> set[str]:
    """Return every turn after an explicit role-switch request.

    Both channels are quarantined because generated benchmark conversations may
    alternate a simulated user and simulated assistant after the switch.
    """
    role_play = False
    result: set[str] = set()
    for line in content.splitlines():
        match = re.match(r"^(TURN_\d+) \[(user|assistant)\]:(.*)$", line, re.IGNORECASE)
        if not match:
            continue
        turn_id, role, text = match.group(1), match.group(2).lower(), match.group(3).lower()
        if role == "user" and "respond as the user" in text:
            role_play = True
            continue
        if role_play:
            result.add(turn_id)
    return result


def typed_value_issues(event: dict) -> tuple[str, ...]:
    value_type = event.get("value_type")
    issues: list[str] = []
    if value_type in {"ENTITY", "TEXT"} and not event.get("object_text"):
        issues.append("MISSING_OBJECT_TEXT")
    if value_type in {"NUMBER", "MONEY", "DURATION"}:
        scalar = event.get("numeric_value") is not None
        range_complete = event.get("numeric_min") is not None and event.get("numeric_max") is not None
        range_partial = (event.get("numeric_min") is None) != (event.get("numeric_max") is None)
        if not scalar and not range_complete:
            issues.append("INCOMPLETE_NUMERIC_VALUE" if range_partial else "MISSING_NUMERIC_VALUE")
        elif range_complete:
            issues.append("RANGE_VALUE_UNSUPPORTED_BY_SCALAR_ALGEBRA")
    if value_type in {"MONEY", "DURATION"} and not event.get("unit"):
        issues.append("MISSING_UNIT")
    if value_type == "DATE" and not event.get("date_value"):
        issues.append("MISSING_DATE_VALUE")
    if value_type == "BOOLEAN" and event.get("boolean_value") is None:
        issues.append("MISSING_BOOLEAN_VALUE")
    return tuple(issues)


def classify_event(event: dict, content: str) -> GateDecision:
    """Classify an extracted event without modifying model output."""
    roles = parse_turn_roles(content)
    sources = tuple(event.get("source_turn_ids") or ())
    missing = tuple(source for source in sources if source not in roles)
    if not sources or missing:
        return GateDecision(GateStatus.QUARANTINE, ("INVALID_SOURCE_PROVENANCE",))

    role_play_block = role_play_block_turn_ids(content)
    if set(sources) <= role_play_block:
        return GateDecision(GateStatus.QUARANTINE, ("EXPLICIT_ROLE_PLAY_BLOCK",))

    subject = str(event.get("subject", "")).strip().lower()
    source_roles = {roles[source] for source in sources}
    if subject in {"user", "the user"} and "user" not in source_roles:
        simulated = role_played_user_turn_ids(content)
        reason = ("EXPLICIT_USER_ROLE_PLAY",) if set(sources) <= simulated else (
            "UNSUPPORTED_USER_ATTRIBUTION",)
        return GateDecision(GateStatus.QUARANTINE, reason)

    value_issues = typed_value_issues(event)
    if value_issues:
        return GateDecision(GateStatus.EVIDENCE_ONLY, value_issues)
    return GateDecision(GateStatus.PERSIST, ())
