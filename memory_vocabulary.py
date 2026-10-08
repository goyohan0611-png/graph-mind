"""Shared vocabulary for ingestion, external LLM adapters, and execution."""
RELATIONS = (
    "IDENTITY_NAME", "HAS_ATTRIBUTE", "HAS_PREFERENCE", "HAS_POSSESSION",
    "LOCATED_AT", "LIVES_AT", "WORKS_AT", "HAS_RELATIONSHIP",
    "HAS_HEALTH_STATE", "HAS_PLAN", "PARTICIPATED_IN", "COMPLETED",
    "PURCHASED", "VISITED", "PRACTICES", "COLLECTS", "RECOMMENDED",
    "HAS_QUANTITY", "HAS_COST", "HAS_DURATION", "HAS_DATE",
    "HAS_FREQUENCY", "HAS_SPEED", "OTHER", "ASSISTANT_STATED",
)
ENTITY_TYPES = ("PERSON", "ORGANIZATION", "PLACE", "PHYSICAL_OBJECT", "EVENT",
                "ACTIVITY", "MEDIA", "CONCEPT", "ROLE", "OTHER", "NONE")
CATEGORIES = ("MUSEUM", "GALLERY", "FOOTWEAR", "MEDICATION", "PROPERTY", "FILM")
PARTICIPANT_ROLES = ("SUBJECT", "OBJECT", "BENEFICIARY", "LOCATION", "SOURCE", "TARGET")
FACT_MODES = ("ASSERTED", "PLANNED")

SEMANTICS = """Shared memory vocabulary (contract 1):
HAS_COST: a stored monetary cost, fee, or price. Use this for MONEY values, even when the
underlying activity was a purchase. Put the item/activity in object_text and connect the beneficiary
or item through participants. Do not put a cost solely under HAS_POSSESSION or PURCHASED.
HAS_QUANTITY: an explicitly stored quantity, including stored aggregate counts. Counting
individual occurrences and looking up an explicitly stored aggregate quantity are different plans.
PARTICIPATED_IN: an actual past activity such as a trip, including solo/family travel.
VISITED: an actual visit to a place. HAS_ATTRIBUTE is not a fallback for a completed visit/trip.
LOCATED_AT: actual location of an item/entity, with that item as a participant (SUBJECT).
HAS_PLAN: an intended future activity or state, never evidence that the intended state happened.
ASSERTED vs PLANNED distinguishes actual facts from intentions; current-state queries exclude plans.
relations are alternative predicate choices; entity_terms describe the object/activity, while
participant_terms constrain explicit participants. Never use an entity like Lola as an object-text
filter when the value is a cost of an activity for Lola. Give a participant constraint instead.
Within each lexical filter alternatives are OR; different filters are AND. Avoid redundant filters
and never require a category not supported by the question. Categories are optional, from the
controlled list, and ingestion must ground them in source wording. Do not invent synonyms/aliases.
No shared category or predicate establishes entity links, occurrence identity, or scope completeness.
"""
