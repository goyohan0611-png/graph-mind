"""All 500 packets of the shipped path on the bench's clean cached vectors, for a fresh end-to-end
accuracy number (the 88.8% headline was measured before the CI vector cache was found corrupted;
this checks it on today's code). product_answer_eval.py works from research/ with relative paths,
the bench workflow runs from the repository root: this runs it from research/.

    python research/full500_spans.py                       # CI: packets
    cd research && OPENAI_API_KEY=... python product_answer_eval.py answer \
        --out runs/graph-v9.12-full500-clean --ids runs/graph-v7.0-full500-30/ids.json
"""
from pathlib import Path
import os
import sys

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.argv = [sys.argv[0], "spans", "--out", "runs/graph-v9.12-full500-clean",
            "--ids", "runs/graph-v7.0-full500-30/ids.json",
            "--cache", str(HERE.parent / "bench/.cache/embed-cache.json")]
import product_answer_eval  # noqa: E402

product_answer_eval.main()
