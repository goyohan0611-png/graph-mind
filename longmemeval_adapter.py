"""Validated adapter for the official LongMemEval cleaned JSON files."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Iterable


DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"
QUESTION_TYPES = (
    "single-session-user", "single-session-assistant",
    "single-session-preference", "temporal-reasoning",
    "knowledge-update", "multi-session",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_instances(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list):
        raise ValueError("LongMemEval root must be a list")
    return value


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, DATE_FORMAT)


def validate_instance(instance: dict) -> list[str]:
    errors: list[str] = []
    required = {"question_id", "question_type", "question", "question_date", "answer",
                "answer_session_ids", "haystack_dates", "haystack_session_ids",
                "haystack_sessions"}
    missing = sorted(required - set(instance))
    if missing:
        return [f"missing fields: {missing}"]
    if instance["question_type"] not in QUESTION_TYPES:
        errors.append(f"unknown question_type: {instance['question_type']}")
    try:
        parse_date(instance["question_date"])
    except ValueError as error:
        errors.append(f"bad question_date: {error}")
    lengths = (len(instance["haystack_dates"]), len(instance["haystack_session_ids"]),
               len(instance["haystack_sessions"]))
    if len(set(lengths)) != 1:
        errors.append(f"parallel session lengths differ: {lengths}")
        return errors
    for date in instance["haystack_dates"]:
        try:
            parse_date(date)
        except ValueError as error:
            errors.append(f"bad haystack date: {error}")
            break
    for session in instance["haystack_sessions"]:
        if not isinstance(session, list):
            errors.append("session is not a list")
            break
        for turn in session:
            if turn.get("role") not in {"user", "assistant"}:
                errors.append(f"bad role: {turn.get('role')}")
                break
            if not isinstance(turn.get("content"), str):
                errors.append("turn content is not text")
                break
    return errors


def flatten_turns(instance: dict) -> list[dict]:
    """Convert one benchmark history into an append-only session/turn stream."""
    rows = []
    for session_number, (date, session_id, turns) in enumerate(zip(
            instance["haystack_dates"], instance["haystack_session_ids"],
            instance["haystack_sessions"])):
        timestamp = parse_date(date).isoformat(timespec="minutes")
        for turn_number, turn in enumerate(turns):
            rows.append({
                "question_id": instance["question_id"],
                "session_id": session_id,
                "session_number": session_number,
                "session_timestamp": timestamp,
                "turn_number": turn_number,
                "turn_id": f"{session_id}:turn-{turn_number:03d}",
                "role": turn["role"],
                "content": turn["content"],
                "has_answer": bool(turn.get("has_answer", False)),
            })
    return rows


def evidence_session_ids_from_turns(instance: dict) -> set[str]:
    return {session_id for session_id, turns in zip(instance["haystack_session_ids"],
                                                     instance["haystack_sessions"])
            if any(turn.get("has_answer", False) for turn in turns)}


def select_development_pilot(instances: Iterable[dict], per_type: int = 5) -> list[str]:
    """All abstentions plus a hash-selected fixed non-abstention sample per type."""
    rows = list(instances)
    abstention = [row for row in rows if row["question_id"].endswith("_abs")]
    selected = list(abstention)
    for question_type in QUESTION_TYPES:
        candidates = [row for row in rows
                      if row["question_type"] == question_type
                      and not row["question_id"].endswith("_abs")]
        candidates.sort(key=lambda row: hashlib.sha256(
            row["question_id"].encode("utf-8")).hexdigest())
        if len(candidates) < per_type:
            raise ValueError(f"Not enough {question_type} examples")
        selected.extend(candidates[:per_type])
    return sorted(row["question_id"] for row in selected)
