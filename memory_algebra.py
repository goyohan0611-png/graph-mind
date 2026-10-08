"""Deterministic typed-value algebra for Graph-MIND evidence execution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class MemoryAtom:
    value_type: str
    value: str | float | bool
    evidence_event_ids: tuple[str, ...]
    effective_at: datetime | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        allowed = {"ENTITY", "TEXT", "NUMBER", "MONEY", "DURATION", "DATE", "BOOLEAN"}
        if self.value_type not in allowed:
            raise ValueError(f"Unsupported value type: {self.value_type}")
        if self.value_type in {"NUMBER", "MONEY", "DURATION"} and not isinstance(
                self.value, (int, float)):
            raise ValueError(f"{self.value_type} requires a numeric value")
        if self.value_type in {"MONEY", "DURATION"} and not self.unit:
            raise ValueError(f"{self.value_type} requires a unit")


@dataclass(frozen=True)
class AlgebraResult:
    status: str
    value_type: str | None
    value: str | float | bool | tuple | None
    unit: str | None
    evidence_event_ids: tuple[str, ...]
    reason: str


def unknown(reason: str, evidence: Iterable[str] = ()) -> AlgebraResult:
    return AlgebraResult("UNKNOWN", None, None, None, tuple(evidence), reason)


def _evidence(atoms: Iterable[MemoryAtom]) -> tuple[str, ...]:
    result = []
    seen = set()
    for atom in atoms:
        for event_id in atom.evidence_event_ids:
            if event_id not in seen:
                seen.add(event_id)
                result.append(event_id)
    return tuple(result)


def lookup(atoms: Iterable[MemoryAtom]) -> AlgebraResult:
    values = list(atoms)
    if len(values) != 1:
        return unknown("MISSING_OR_NON_UNIQUE", _evidence(values))
    atom = values[0]
    return AlgebraResult("ANSWER", atom.value_type, atom.value, atom.unit,
                         atom.evidence_event_ids, "COMPLETE")


def latest(atoms: Iterable[MemoryAtom]) -> AlgebraResult:
    values = [atom for atom in atoms if atom.effective_at is not None]
    if not values:
        return unknown("NO_DATED_VALUE")
    winner = max(values, key=lambda atom: atom.effective_at)
    return AlgebraResult("ANSWER", winner.value_type, winner.value, winner.unit,
                         winner.evidence_event_ids, "COMPLETE")


def earliest(atoms: Iterable[MemoryAtom]) -> AlgebraResult:
    values = [atom for atom in atoms if atom.effective_at is not None]
    if not values:
        return unknown("NO_DATED_VALUE")
    winner = min(values, key=lambda atom: atom.effective_at)
    return AlgebraResult("ANSWER", winner.value_type, winner.value, winner.unit,
                         winner.evidence_event_ids, "COMPLETE")


def count(atoms: Iterable[MemoryAtom], distinct: bool = True) -> AlgebraResult:
    values = list(atoms)
    if distinct:
        selected = []
        seen = set()
        for atom in values:
            key = (atom.value_type, atom.value, atom.unit)
            if key not in seen:
                seen.add(key)
                selected.append(atom)
        values = selected
    return AlgebraResult("ANSWER", "NUMBER", float(len(values)), None,
                         _evidence(values), "COMPLETE")


def sum_values(atoms: Iterable[MemoryAtom]) -> AlgebraResult:
    values = list(atoms)
    if not values:
        return unknown("NO_VALUES")
    if any(atom.value_type not in {"NUMBER", "MONEY", "DURATION"} for atom in values):
        return unknown("NON_NUMERIC_VALUE", _evidence(values))
    value_types = {atom.value_type for atom in values}
    units = {atom.unit for atom in values}
    if len(value_types) != 1 or len(units) != 1:
        return unknown("INCOMPATIBLE_TYPES_OR_UNITS", _evidence(values))
    return AlgebraResult("ANSWER", values[0].value_type,
                         float(sum(float(atom.value) for atom in values)),
                         values[0].unit, _evidence(values), "COMPLETE")


def subtract(left: MemoryAtom, right: MemoryAtom) -> AlgebraResult:
    values = [left, right]
    if any(atom.value_type not in {"NUMBER", "MONEY", "DURATION"} for atom in values):
        return unknown("NON_NUMERIC_VALUE", _evidence(values))
    if left.value_type != right.value_type or left.unit != right.unit:
        return unknown("INCOMPATIBLE_TYPES_OR_UNITS", _evidence(values))
    return AlgebraResult("ANSWER", left.value_type, float(left.value) - float(right.value),
                         left.unit, _evidence(values), "COMPLETE")


def compare(left: MemoryAtom, right: MemoryAtom) -> AlgebraResult:
    values = [left, right]
    if left.value_type != right.value_type or left.unit != right.unit:
        return unknown("INCOMPATIBLE_TYPES_OR_UNITS", _evidence(values))
    if left.value == right.value:
        result = "EQUAL"
    elif left.value < right.value:
        result = "LEFT_LESS"
    else:
        result = "LEFT_GREATER"
    return AlgebraResult("ANSWER", "TEXT", result, None, _evidence(values), "COMPLETE")


def date_diff(start: MemoryAtom, end: MemoryAtom, unit: str = "days") -> AlgebraResult:
    if (start.value_type != "DATE" or end.value_type != "DATE"
            or not isinstance(start.value, str) or not isinstance(end.value, str)):
        return unknown("DATE_REQUIRED", _evidence([start, end]))
    try:
        start_date = datetime.fromisoformat(start.value)
        end_date = datetime.fromisoformat(end.value)
    except ValueError:
        return unknown("INVALID_ISO_DATE", _evidence([start, end]))
    seconds = (end_date - start_date).total_seconds()
    divisors = {"seconds": 1, "hours": 3600, "days": 86400, "weeks": 604800}
    if unit not in divisors:
        return unknown("UNSUPPORTED_DATE_UNIT", _evidence([start, end]))
    return AlgebraResult("ANSWER", "DURATION", seconds / divisors[unit], unit,
                         _evidence([start, end]), "COMPLETE")


def list_values(atoms: Iterable[MemoryAtom]) -> AlgebraResult:
    values = list(atoms)
    if not values:
        return unknown("NO_VALUES")
    return AlgebraResult("ANSWER", "LIST", tuple(atom.value for atom in values), None,
                         _evidence(values), "COMPLETE")
