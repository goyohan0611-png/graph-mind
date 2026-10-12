"""Explicit state cardinality and trusted, source-bound intake grants.

The grant is a backend review operation, never an LLM-provided permission.
Literal spans prove provenance; semantics/identity remain attestation responsibilities.
"""
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

VERSION = 'state-relations-v1'

@dataclass(frozen=True)
class RelationPolicy:
    cardinality: str
    meaning: str

POLICIES = MappingProxyType({
    'LOCATED_AT': RelationPolicy('SINGLE', 'Location in an explicitly single-location state scope'),
    'PRIMARY_RESIDENCE': RelationPolicy('SINGLE', 'Explicitly designated primary residence'),
    'PRIMARY_WORKPLACE': RelationPolicy('SINGLE', 'Explicitly designated primary workplace'),
    'PRIMARY_VEHICLE_TYPE': RelationPolicy('SINGLE', 'Explicitly designated primary vehicle type, not ownership'),
    'LIVES_AT': RelationPolicy('MULTI', 'Residence without a unique primary designation'),
    'WORKS_AT': RelationPolicy('MULTI', 'Workplace without a unique primary designation'),
    'USES_VEHICLE_TYPE': RelationPolicy('MULTI', 'Vehicle usage, not ownership or primary designation'),
    'OWNS_VEHICLE_TYPE': RelationPolicy('MULTI', 'Explicit ownership of vehicle types'),
    'HAS_POSSESSION': RelationPolicy('MULTI', 'Explicit possession of individually identified items'),
})
SINGLE_STATE_RELATIONS = tuple(k for k, p in POLICIES.items() if p.cardinality == 'SINGLE')

def candidate_fingerprint(candidate):
    return hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()

@dataclass(frozen=True)
class StateIntakeGrant:
    candidate_sha256: str
    memory_id: str
    source_id: str
    entity_id: str
    relation: str
    context_span_id: str
    subject_span_id: str
    value_span_id: str
    semantics_attestation_id: str
    identity_attestation_id: str
    valid_from: str | None = None
    time_span_id: str | None = None
    observed_at: str | None = None
    observation_attestation_id: str | None = None

def single_state_relation(relation):
    return isinstance(relation, str) and relation in SINGLE_STATE_RELATIONS

def compile_state_intake(candidate, *, grant, catalog, source, known_at, clock='SOURCE_LOCAL'):
    """Return event only for a reviewed atomic state with sufficient clock bindings.

    source['session_timestamp'] is authoritative backend-normalized source metadata.
    An explicit instant is required for changes in v1; partial/date-only times are held.
    No grant is synthesized, no completeness/carry certificate is issued here.
    """
    from temporal_state import timestamp
    reasons = []
    def outcome(disposition, reason_list, event=None):
        return {'disposition': disposition, 'reasons': sorted(set(reason_list)),
            'event': event, 'state_contract_version': VERSION, 'certificate_issued': False}

    kind = candidate.get('kind')
    if kind in {'PLAN', 'PLAN_CANCEL'}:
        return outcome('CONTROL_EVIDENCE_ONLY', ['INTENTION_DOES_NOT_MUTATE_STATE'])
    if kind == 'CORRECTION':
        return outcome('TARGET_REVIEW_REQUIRED', ['EXPLICIT_RECORD_TARGET_AND_REVISION_REQUIRED'])
    if kind not in {'OBSERVATION', 'STATE_CHANGE'}:
        return outcome('HOLD', ['UNSUPPORTED_OR_AMBIGUOUS_MODE'])
    if not isinstance(grant, StateIntakeGrant):
        return outcome('HOLD', ['TRUSTED_SOURCE_SEMANTIC_IDENTITY_GRANT_REQUIRED'])
    if grant.candidate_sha256 != candidate_fingerprint(candidate):
        reasons.append('CANDIDATE_GRANT_MISMATCH')
    if (grant.memory_id, grant.source_id) != (source.get('memory_id'), source.get('source_id')):
        reasons.append('SOURCE_SCOPE_GRANT_MISMATCH')
    for key in ['entity_id', 'semantics_attestation_id', 'identity_attestation_id']:
        value = getattr(grant, key)
        if not isinstance(value, str) or not value.strip():
            reasons.append(key.upper() + '_REQUIRED')
    policy = POLICIES.get(grant.relation) if isinstance(grant.relation, str) else None
    if policy is None:
        reasons.append('UNSUPPORTED_STATE_RELATION')
    elif policy.cardinality != 'SINGLE':
        reasons.append('MULTI_VALUE_EXECUTOR_REQUIRED')
    spans = {}
    for name in ['context', 'subject', 'value'] + (['time'] if grant.time_span_id else []):
        try:
            span = catalog.get(getattr(grant, name + '_span_id'))
            spans[name] = span
            if (span['memory_id'], span['session_id'], span['source_turn_id'], span['source_role']) != (
                    grant.memory_id, grant.source_id, candidate.get('turn_id'), 'user'):
                reasons.append('SPAN_SOURCE_ROLE_SCOPE_MISMATCH')
        except (ValueError, KeyError, TypeError):
            reasons.append('SOURCE_SPAN_NOT_RESTORABLE')
    context = spans.get('context')
    if context:
        if context['text'] != candidate.get('support_quote'):
            reasons.append('CONTEXT_CLAIM_MISMATCH')
        for name in ['subject', 'value', 'time']:
            span = spans.get(name)
            if span and not (context['start'] <= span['start'] < span['end'] <= context['end']):
                reasons.append('ATOM_OUTSIDE_CLAIM_CONTEXT')
        for name in ['subject', 'value']:
            span = spans.get(name)
            if span and span['text'] not in (candidate.get(name + '_text') or ''):
                reasons.append('ATOM_CANDIDATE_MISMATCH')
    try:
        recorded = timestamp(known_at, clock)
    except (ValueError, TypeError):
        reasons.append('INVALID_KNOWLEDGE_TIMESTAMP')
        recorded = None
    if kind == 'STATE_CHANGE':
        time_span = spans.get('time')
        if not time_span or time_span['text'] != grant.valid_from:
            reasons.append('LITERAL_VALID_INSTANT_REQUIRED')
        try:
            valid = timestamp(grant.valid_from, clock)
            if recorded is not None and valid > recorded:
                reasons.append('STATE_CHANGE_AFTER_RECORDING')
        except (ValueError, TypeError):
            reasons.append('VALID_TIME_UNRESOLVED')
    else:
        if (candidate.get('time_kind') != 'SESSION_OBSERVATION' or not grant.observation_attestation_id
                or grant.observed_at != source.get('session_timestamp')):
            reasons.append('CURRENT_SOURCE_OBSERVATION_BINDING_REQUIRED')
        try:
            observed = timestamp(grant.observed_at, clock)
            if recorded is not None and observed > recorded:
                reasons.append('OBSERVATION_AFTER_RECORDING')
        except (ValueError, TypeError):
            reasons.append('OBSERVATION_TIME_UNRESOLVED')
    if reasons:
        return outcome('HOLD', reasons)
    event = {'memory_id': grant.memory_id,
        'evidence_id': 'state_' + candidate_fingerprint({'candidate': candidate,
            'source_id': grant.source_id, 'memory_id': grant.memory_id,
            'relation': grant.relation, 'entity_id': grant.entity_id,
            'value': spans['value']['text'], 'valid_from': grant.valid_from if kind == 'STATE_CHANGE' else None,
            'observed_at': grant.observed_at if kind == 'OBSERVATION' else None}),
        'relation': grant.relation, 'object_text': spans['value']['text'],
        'fact_mode': 'ASSERTED', 'state_kind': kind, 'source_roles': ['user'],
        'source_turn_ids': [candidate['turn_id']], 'recorded_at': known_at,
        'participants': [{'role': 'SUBJECT', 'entity_id': grant.entity_id,
            'name': spans['subject']['text'], 'source_role': 'user'}],
        'state_contract': {'version': VERSION, 'cardinality': 'SINGLE',
            'semantics_attestation_id': grant.semantics_attestation_id,
            'identity_attestation_id': grant.identity_attestation_id,
            'source_id': grant.source_id, 'source_span_ids': {k: s['span_id'] for k, s in spans.items()}}}
    if kind == 'STATE_CHANGE':
        event.update(state_valid_from=grant.valid_from, state_time_verified=True)
    else:
        event.update(observed_at=grant.observed_at, observation_time_verified=True)
    return outcome('READY_FOR_TRUSTED_APPEND', [], event)
