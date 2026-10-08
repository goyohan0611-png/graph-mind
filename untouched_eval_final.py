"""Untouched evaluation v2 of the FROZEN v3.2 pipeline — evaluate ONCE.

v1 (65.8%) exposed pilot overfitting; its 120 questions then became the dev set that v3.1 (78.3%)
and v3.2 (85.0%) were tuned on. This draws 120 questions never used in any experiment (pilot 60,
moat 36 and the v1/dev 120 are all excluded), pre-registers the question ids, config, code hashes
and the interpretation thresholds BEFORE running, and refuses to run if the frozen code drifts.

Pre-registered reading of the result (answerable-only; dev v3.2 = 85.0%):
    >= 82%   strong: the dev1+dev2 combined 82.1% held up
    78-82%   solid: clears Zep 71.2% and MemPalace's honest QA ~67.2% with room to spare
    73-78%   flat: no real gain since untouched v2 (77.5%)
    <  73%   the weapons were dev-specific

    OPENAI_API_KEY=... python untouched_eval_v2.py
"""
from pathlib import Path
import json
import os
import random
import sys

import longmemeval_operator_diagnostic as planner
import longmemeval_retrieval_executor as pipe
import official_judge_v073 as judge
import untouched_eval_v1 as v1
import untouched_eval_v2 as v2
import untouched_eval_v3 as v3
from longmemeval_adapter import load_instances
from moat_rag_diagnostic import _select as moat_select

OUT = Path("runs/graph-v4.2-untouched-final")
N = 72    # every question left in the pool: this is the last fresh set in LongMemEval_S
SEED = 20260923
THRESHOLDS = {">=0.85": "k=16 + multi-view + aliases generalised fully",
              "0.80-0.85": "strong: clear gain over untouched v3 (75.0%)",
              "0.75-0.80": "modest gain, still above Zep 71.2% and MemPalace QA ~67.2%",
              "<0.75": "the retrieval work did not transfer"}


def freeze():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "preregistration.json"
    data = load_instances(pipe.DATA)
    used = set(json.loads(pipe.PILOT.read_text(encoding="utf-8"))["question_ids"])
    used |= {row["question_id"] for row in moat_select(data)}
    used |= set(json.loads((v1.OUT / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    used |= set(json.loads((v2.OUT / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    used |= set(json.loads((v3.OUT / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    pool = sorted(row["question_id"] for row in data if row["question_id"] not in used)
    ids = sorted(random.Random(SEED).sample(pool, N))
    manifest = {"role": "FINAL untouched evaluation of the frozen v4.2 pipeline (evaluate once; pool exhausted)",
                "n": N, "seed": SEED, "pool_size": len(pool), "excluded_ids": len(used),
                "question_ids": ids, "thresholds": THRESHOLDS,
                "dev_reference": {"v1_untouched": 0.658, "v2_untouched": 0.775, "v3.2_dev": 0.850, "v3.7_dev1_dev2_combined": 0.821, "untouched_v3_k8": 0.750, "v3.9_dev2_k16_multiview": 0.858},
                "config": {"READER_MODEL": pipe.READER_MODEL, "GATE_MODE": pipe.GATE_MODE, "MULTI_VIEW": pipe.MULTI_VIEW, "search_aliases": True, "engine_arithmetic": sorted(pipe.AGGREGATE_OPERATORS),
                           "GATE_MODEL": pipe.GATE_MODEL, "RETRIEVAL_K": pipe.RETRIEVAL_K,
                           "retrieval_index": "user turns only (assistant added for assistant-history Qs)",
                           "judge": judge.JUDGE_MODEL, "planner": planner.MODEL},
                "code_sha256": {f: v1._sha(f) for f in v1.FROZEN_FILES}}
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("question_ids", "config", "code_sha256"):
            if old[key] != manifest[key]:
                sys.exit(f"FROZEN {key} CHANGED since freeze — refusing to evaluate")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return ids


def main():
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("OPENAI_API_KEY_REQUIRED")
    v1._memory_only_cache()
    ids = set(freeze())
    v1.OUT, plans = OUT, None  # reuse v1's planner runner against this manifest
    plans = v1.make_plans(key, sorted(ids))
    pipe.OUTPUT, pipe.OPERATOR_RESPONSES = OUT, plans
    pipe.select_questions = lambda: sorted(
        (r for r in load_instances(pipe.DATA) if r["question_id"] in ids),
        key=lambda r: r["question_id"])
    questions, sessions = pipe.prepare()
    v1._flush_cache()
    pipe.run_ingestion(key, 8, sessions)
    try:
        pipe.run_answers(key, 4, questions, sessions)
    finally:
        v1._flush_cache()
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    print(json.dumps(judge.run()["by_type"], indent=1))


if __name__ == "__main__":
    main()
