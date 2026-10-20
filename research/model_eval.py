"""Smaller exports of the embedding models (int8, files already in their Hugging Face repos)
against the shipped MiniLM, in one CI job: the Korean probe (model_probe.py: ranks, speed,
memory, one question's time) and English evidence on every 5th LongMemEval_S question
(bench/evidence.py, 100 questions). mpnet finds Korean questions best but is 3.4x slower and
1 GB in fp32; int8 is meant to keep its accuracy at a quarter of the size.

    gh workflow run bench -f research=model_eval.py
"""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "research/runs/graph-v9.13-int8-models"
MPNET = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
MINI = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
INT8 = "@onnx/model_quint8_avx2.onnx"          # any x86-64 CPU since 2013


def run(*args):
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    run("research/model_probe.py", "distractors")
    for model in (MINI, MPNET + INT8, MPNET + "@onnx/model_qint8_avx512_vnni.onnx", MINI + INT8):
        run("research/model_probe.py", "run", model)
    shutil.copy(ROOT / "research/runs/graph-v9.8-model-probe/results.jsonl", OUT / "probe.jsonl")
    for model in (None, MPNET + INT8, MINI + INT8):     # the shipped one's vectors are cached
        run("bench/evidence.py", "--every", "5", *(["--model", model] if model else []))
        for result in (ROOT / "bench/.cache").glob("evidence-*.json"):
            shutil.copy(result, OUT / result.name)
