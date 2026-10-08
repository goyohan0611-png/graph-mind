"""Does the score follow the engine or the reader?

Same questions, same retrieval, same events, same evidence packet — only the model that writes the
answer changes. This is the claim the project has made from the start ("모델은 바꿔도, 기억은 내 것")
and has never measured. For reference, Mastra's own numbers move 10.6 points between readers
(gpt-4o 84.23% -> gpt-5-mini 94.87%), because their design hands the model 30k tokens of compressed
log and lets it do the work.

    OPENAI_API_KEY=... python reader_swap.py gpt-4o gpt-4o-mini
    python reader_swap.py --report
"""
from argparse import ArgumentParser
from pathlib import Path
import json
import os
import shutil

import longmemeval_retrieval_executor as pipe
import official_judge_v073 as judge
from longmemeval_adapter import load_instances

SRC = Path("runs/graph-v3.8-dev2-k16")            # k=16 ingestion + embedding cache to reuse
IDS = Path("runs/graph-v3.2-untouched-eval-v2")   # the dev2 question list
ROOT = Path("runs/graph-v4.5-reader-swap")
BASELINE = ("gpt-5-mini", Path("runs/graph-v3.9-dev2-mv"))


def run(model: str):
    out = ROOT / model.replace(".", "_")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("ingestion.jsonl", "plans.jsonl"):
        if not (out / name).exists():
            shutil.copyfile(SRC / name, out / name)
    if not (out / "embed-cache-local.vec").exists():
        shutil.copytree(SRC / "embed-cache-local.vec", out / "embed-cache-local.vec")
    ids = set(json.loads((IDS / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    pipe.OUTPUT, pipe.OPERATOR_RESPONSES, pipe.READER_MODEL = out, out / "plans.jsonl", model
    pipe.select_questions = lambda: sorted(
        (r for r in load_instances(pipe.DATA) if r["question_id"] in ids),
        key=lambda r: r["question_id"])
    questions, sessions = pipe.prepare()
    key = os.environ["OPENAI_API_KEY"]
    try:
        pipe.run_answers(key, 4, questions, sessions)
    finally:
        pipe._EMBEDDER and pipe._EMBEDDER.flush()
    judge.SRC, judge.OUT = out, out / "official-judge.jsonl"
    return judge.run()["by_type"]["ALL"]


def report():
    rows = [(BASELINE[0], BASELINE[1])]
    rows += [(d.name.replace("_", "."), d) for d in sorted(ROOT.glob("*")) if d.is_dir()]
    table = {}
    for model, directory in rows:
        path = directory / "official-judge-report.json"
        if path.exists():
            all_row = json.loads(path.read_text(encoding="utf-8"))["by_type"]["ALL"]
            table[model] = {"correct": all_row["correct"], "n": all_row["n"],
                            "acc": round(all_row["acc"] * 100, 1)}
    if table:
        best, worst = max(table.values(), key=lambda r: r["acc"]), min(table.values(), key=lambda r: r["acc"])
        table["spread_points"] = round(best["acc"] - worst["acc"], 1)
    print(json.dumps(table, indent=1))
    (ROOT / "report.json").write_text(json.dumps(table, indent=1), encoding="utf-8")


def main():
    parser = ArgumentParser()
    parser.add_argument("models", nargs="*")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    for model in args.models:
        print(model, run(model), flush=True)
    if args.report or not args.models:
        report()


if __name__ == "__main__":
    main()
