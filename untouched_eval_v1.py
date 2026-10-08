"""Untouched LongMemEval evaluation of the FROZEN Graph-MIND pipeline.

Every lever so far (COUNT/abstain prompt, evidence passages, assistant regex, timeline hints, slim
events, gate) was tuned while looking at the same 60 pilot questions, so 88-91% there is a DEV score.
This draws questions never used in any experiment, freezes the question list + pipeline code hashes +
config BEFORE running, and evaluates exactly once with the official LongMemEval judge protocol.

    OPENAI_API_KEY=... python untouched_eval_v1.py
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import random
import sys

import longmemeval_operator_diagnostic as planner
import longmemeval_retrieval_executor as pipe
import official_judge_v073 as judge
from longmemeval_adapter import load_instances
from moat_rag_diagnostic import _select as moat_select

OUT = Path("runs/graph-v3.0-untouched-eval-v1")
N = 120
SEED = 20260918
FROZEN_FILES = ["longmemeval_retrieval_executor.py", "semantic_grounding_v073.py",
                "official_judge_v073.py", "ingestion_gate.py", "precondition_verifier.py"]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "preregistration.json"  # pipe.prepare() writes its own freeze.json
    data = load_instances(pipe.DATA)
    used = set(json.loads(pipe.PILOT.read_text(encoding="utf-8"))["question_ids"])
    used |= {row["question_id"] for row in moat_select(data)}
    pool = sorted(row["question_id"] for row in data if row["question_id"] not in used)
    ids = sorted(random.Random(SEED).sample(pool, N))
    manifest = {"role": "untouched evaluation of the frozen pipeline (evaluate once)",
                "n": N, "seed": SEED, "excluded_ids": len(used), "question_ids": ids,
                "config": {"READER_MODEL": pipe.READER_MODEL, "GATE_ENABLED": pipe.GATE_ENABLED,
                           "GATE_MODEL": pipe.GATE_MODEL, "RETRIEVAL_K": pipe.RETRIEVAL_K,
                           "judge": judge.JUDGE_MODEL, "planner": planner.MODEL},
                "code_sha256": {f: _sha(f) for f in FROZEN_FILES}}
    if manifest_path.exists():  # re-entry: refuse if anything drifted since the freeze
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("question_ids", "config", "code_sha256"):
            if old[key] != manifest[key]:
                sys.exit(f"FROZEN {key} CHANGED since freeze — refusing to evaluate")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return ids


def make_plans(key, ids):
    path = OUT / "plans.jsonl"
    done = pipe.load_jsonl(path, "question_id")
    rows = [r for r in load_instances(pipe.DATA) if r["question_id"] in set(ids)
            and r["question_id"] not in done]

    def send(row):
        rec = pipe.safe_call(key, planner.payload(row), {"question_id": row["question_id"]},
                             lambda raw: json.loads(pipe.response_text(raw)))
        return {"question_id": row["question_id"], "status": rec["status"],
                "plan": rec.get("result")}
    pipe.run_parallel(rows, path, 8, send)
    return path


def _memory_only_cache():
    """No-op since LocalEmbedder moved to the float32 VectorCache (vector_cache.py), which already
    holds vectors compactly and flushes every FLUSH_EVERY writes. Kept so the older runners import."""


def _flush_cache():
    if pipe._EMBEDDER is not None:
        pipe._EMBEDDER.flush()


def main():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY_REQUIRED")
    _memory_only_cache()
    ids = freeze()
    plans_path = make_plans(key, ids)
    # point the frozen pipeline at the untouched questions (code unchanged, only inputs)
    pipe.OUTPUT = OUT
    pipe.OPERATOR_RESPONSES = plans_path
    manifest_ids = set(ids)
    pipe.select_questions = lambda: sorted(
        (r for r in load_instances(pipe.DATA) if r["question_id"] in manifest_ids),
        key=lambda r: r["question_id"])
    questions, sessions = pipe.prepare()
    _flush_cache()
    pipe.run_ingestion(key, 8, sessions)
    try:
        pipe.run_answers(key, 4, questions, sessions)
    finally:
        _flush_cache()
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    report = judge.run()
    print(json.dumps(report["by_type"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
