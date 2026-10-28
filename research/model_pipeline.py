"""Everything a new embedding model needs before it can ship, in one CI job: LoCoMo pools (memory
1x, and 10x with 200 per search), LongMemEval_S pools, then re-ranker weights trained on both with
held-out recall (rerank_v2_check.py --ship). Progress lives in bench/.cache (vectors).

    gh workflow run bench -f research="model_pipeline.py REPO@onnx/model_int8.onnx"
"""
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
model = sys.argv[1]
suffix = "-" + model.split("/")[1].split("@")[0]
for step in (["locomo_pools.py", "--model", model],
             ["locomo_pools.py", "--model", model, "--mix", "9", "--pool", "200"],
             ["lme_pools.py", "--model", model],
             ["rerank_v2_check.py", "--ship", "--suffix", suffix]):
    print("==", " ".join(step), flush=True)
    subprocess.run([sys.executable, str(HERE / step[0]), *step[1:]], check=True)
