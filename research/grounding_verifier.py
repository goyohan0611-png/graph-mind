"""Engine-level abstention: verify the ANSWER against the evidence, locally.

The LLM sufficiency gate asked a model "could this be answered?" before the answer existed. It cost
a second LLM call, it made abstention depend on that model, and on untouched v2 it killed 3 correct
answers (77.5% with it, 80.0% without). This verifies the opposite direction — the answer the reader
actually produced must be *supported* by the evidence:

  * numbers and dates in the answer must appear in the evidence (a reader inventing "$8,750" or
    "January 15th" is the confabulation the gate existed to stop) — except values the ENGINE itself
    computed from the reader's instance list, which are grounded by construction;
  * otherwise the answer must be semantically close to some evidence passage (local embeddings, so
    paraphrases and non-English answers pass where token overlap would not).

No API call, no model dependency, deterministic given the same evidence.

    python grounding_verifier.py   # self-check
"""
from __future__ import annotations

import re

THRESHOLD = 0.35          # same bar the product's semantic recall gate uses
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STOP_NUMBERS = {"0", "1", "2"}  # tiny counts are usually reasoning output, not quoted evidence


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".").lstrip("0") or "0" for n in _NUM.findall(text or "")}


def numbers_supported(answer: str, evidence: str) -> bool:
    """Every non-trivial number in the answer must occur in the evidence."""
    have = _numbers(evidence)
    return all(n in have for n in _numbers(answer) - _STOP_NUMBERS)


def verify(result: dict, evidence_texts: list[str], embedder=None,
           threshold: float = THRESHOLD) -> tuple[bool, dict]:
    """(supported, detail). Abstentions stay abstentions; engine-computed aggregates skip the
    number check because the engine derived them from the reader's grounded instance list."""
    if result.get("abstained"):
        return False, {"reason": "reader_abstained"}
    answer = (result.get("answer") or "").strip()
    if not answer or answer.upper() == "UNKNOWN":
        return False, {"reason": "empty_answer"}
    evidence = "\n".join(evidence_texts)
    if not result.get("aggregate_mode") and not numbers_supported(answer, evidence):
        return False, {"reason": "unsupported_number",
                       "answer_numbers": sorted(_numbers(answer) - _STOP_NUMBERS)}
    if embedder is None or not evidence_texts:
        return True, {"reason": "numbers_ok"}
    from semantic_grounding_v073 import cosine
    vectors = embedder.embed([answer] + evidence_texts)
    best = max(cosine(vectors[0], v) for v in vectors[1:])
    return best >= threshold, {"reason": "semantic", "best": round(best, 3)}


def _self_check():
    evidence = ["I finally beat the last boss in the Dark Souls 3 DLC last weekend.",
                "I raised $600 at the charity yoga event and $3,150 at the bike-a-thon."]
    ok, _ = verify({"answer": "Dark Souls 3 DLC", "abstained": False}, evidence)
    assert ok
    ok, detail = verify({"answer": "I raised $8,750 in total.", "abstained": False}, evidence)
    assert not ok and detail["reason"] == "unsupported_number", detail
    ok, _ = verify({"answer": "$3,750", "abstained": False, "aggregate_mode": "sum"}, evidence)
    assert ok, "engine-computed totals are grounded by construction"
    ok, detail = verify({"answer": "UNKNOWN", "abstained": True}, evidence)
    assert not ok and detail["reason"] == "reader_abstained"
    ok, _ = verify({"answer": "$600 at the yoga event", "abstained": False}, evidence)
    assert ok
    assert numbers_supported("3 events", "there were 3 events") is True
    assert numbers_supported("38 coins", "I have 37 coins") is False
    assert numbers_supported("two items", "no digits here") is True  # words are not checked
    print("grounding_verifier self-check ok")


if __name__ == "__main__":
    _self_check()
