"""Meaning search in Korean vs English: the shipped embedding model against multilingual-e5.

For the four translated LoCoMo conversations (locomo_ko.py) and the same four in English, every
turn and question is embedded and each question ranks its own conversation's turns by cosine.
Reported: the answer turn in the first 10 / 30 / 50. Runs at the lowest CPU priority, two
threads. e5 models expect "query: " / "passage: " before the text.

    python research/embed_probe.py
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "research"))

from locomo_pools import DATA, evidence_ids, korean, turns_of  # noqa: E402
import numpy as np  # noqa: E402

CONVS = ("conv-26", "conv-30", "conv-41", "conv-42")
MODELS = [("sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
           "tokenizer.json", "onnx/model_quint8_avx2.onnx", "", ""),
          ("intfloat/multilingual-e5-small", "onnx/tokenizer.json", "onnx/model.onnx",
           "query: ", "passage: ")]


def encoder(repo, tok_file, model_file):
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer
    import onnxruntime
    tok = Tokenizer.from_file(hf_hub_download(repo, tok_file))
    tok.enable_truncation(256)
    tok.enable_padding()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    session = onnxruntime.InferenceSession(hf_hub_download(repo, model_file), options,
                                           providers=["CPUExecutionProvider"])
    names = {i.name for i in session.get_inputs()}

    def encode(texts):
        out = []
        for start in range(0, len(texts), 32):
            batch = tok.encode_batch(texts[start:start + 32])
            ids = np.array([b.ids for b in batch], dtype=np.int64)
            mask = np.array([b.attention_mask for b in batch], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = session.run(None, feed)[0]
            w = mask[..., None].astype(np.float32)
            v = (hidden * w).sum(1) / np.clip(w.sum(1), 1e-9, None)
            out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.concatenate(out)
    return encode


def main() -> None:
    import psutil
    psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS if sys.platform == "win32" else 19)
    english = [s for s in json.loads(DATA.read_text(encoding="utf-8")) if s["sample_id"] in CONVS]
    sets = {"en": english, "ko": korean(english)}
    for repo, tok_file, model_file, q_pre, p_pre in MODELS[1 if "--e5" in sys.argv else 0:]:
        encode = encoder(repo, tok_file, model_file)
        for lang, samples in sets.items():
            hits, total = {10: 0, 30: 0, 50: 0}, 0
            for sample in samples:
                turns = list(turns_of(sample))
                ids = [t for t, _, _ in turns]
                docs = encode([p_pre + text for _, text, _ in turns])
                qa = [q for q in sample["qa"] if q["category"] != 5 and evidence_ids(q)]
                queries = encode([q_pre + q["question"] for q in qa])
                for q, vector in zip(qa, queries):
                    order = np.argsort(-(docs @ vector))
                    gold = set(evidence_ids(q))
                    first = next((r for r, i in enumerate(order) if ids[i] in gold), None)
                    total += 1
                    for k in hits:
                        hits[k] += first is not None and first < k
            print(f"{repo.split('/')[1]:<40} {lang} {total} q  "
                  + "  ".join(f"top{k} {v / total:.1%}" for k, v in hits.items()), flush=True)


if __name__ == "__main__":
    main()
