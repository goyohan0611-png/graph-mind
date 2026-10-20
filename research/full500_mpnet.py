"""All 500 packets on the 0.9.10 model (multilingual mpnet int8), for the headline measured on
MiniLM (88.8%). Embedding all of LongMemEval_S takes longer than one CI job: progress and vectors
live in bench/.cache, which the bench workflow saves after every run, so dispatching it again
continues where it stopped. Then answers and judging run locally:

    gh workflow run bench -f research=full500_mpnet.py         # until it prints "500 packets"
    cd research && OPENAI_API_KEY=... python product_answer_eval.py answer \
        --out runs/graph-v9.14-full500-mpnet8 --ids runs/graph-v7.0-full500-30/ids.json
"""
from pathlib import Path
import os
import shutil
import sys

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "bench/.cache"
WORK = CACHE / "full500-mpnet8"                      # kept between CI runs
os.chdir(HERE)
sys.path.insert(0, str(HERE.parent / "src"))
from graph_mind.local_embedder import LOCAL_MODEL  # noqa: E402

slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")   # bench/evidence.py's cache for it
sys.argv = [sys.argv[0], "spans", "--out", str(WORK), "--writable",
            "--ids", "runs/graph-v7.0-full500-30/ids.json",
            "--cache", str(CACHE / f"embed-{slug}.json")]
import product_answer_eval  # noqa: E402

try:
    product_answer_eval.main()
finally:
    out = HERE / "runs/graph-v9.14-full500-mpnet8"
    shutil.copytree(WORK, out, dirs_exist_ok=True)
    spans = WORK / "spans.jsonl"
    count = sum(1 for _ in spans.open(encoding="utf-8")) if spans.exists() else 0
    print(f"{count} packets")
