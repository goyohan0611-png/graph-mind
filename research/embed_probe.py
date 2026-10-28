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
BGE = "Represent this sentence for searching relevant passages: "
QWEN = "Instruct: Given a question, retrieve past conversation turns that answer it\nQuery:"
# (repo, tokenizer, onnx, query prefix, passage prefix, pooling)
MODELS = [("sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
           "tokenizer.json", "onnx/model_quint8_avx2.onnx", "", "", "mean"),
          ("intfloat/multilingual-e5-small", "onnx/tokenizer.json", "onnx/model.onnx",
           "query: ", "passage: ", "mean"),
          ("intfloat/multilingual-e5-base", "tokenizer.json", "onnx/model_qint8_avx512_vnni.onnx",
           "query: ", "passage: ", "mean"),
          ("Snowflake/snowflake-arctic-embed-m-v2.0", "tokenizer.json", "onnx/model_int8.onnx",
           "query: ", "", "cls"),
          ("onnx-community/Qwen3-Embedding-0.6B-ONNX", "tokenizer.json", "onnx/model_int8.onnx",
           QWEN, "", "last"),
          ("ibm-granite/granite-embedding-278m-multilingual", "tokenizer.json", "model.onnx",
           "", "", "cls"),
          ("onnx-community/embeddinggemma-300m-ONNX", "tokenizer.json", "onnx/model_q4.onnx",
           "task: search result | query: ", "title: none | text: ", "sentence"),
          ("Xenova/bge-small-en-v1.5", "tokenizer.json", "onnx/model_int8.onnx",
           BGE, "", "cls"),
          ("Snowflake/snowflake-arctic-embed-s", "tokenizer.json", "onnx/model_int8.onnx",
           BGE, "", "cls")]


def encoder(repo, tok_file, model_file, pooling="mean"):
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer
    import onnxruntime
    tok = Tokenizer.from_file(hf_hub_download(repo, tok_file))
    tok.enable_truncation(256)
    tok.enable_padding()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    graph = hf_hub_download(repo, model_file)
    try:                                              # weights kept beside the graph: ONNX
        data = hf_hub_download(repo, model_file + "_data")   # Runtime wants them in one folder
        import shutil
        import tempfile
        folder = Path(tempfile.mkdtemp())
        shutil.copy(graph, folder / Path(model_file).name)
        shutil.copy(data, folder / (Path(model_file).name + "_data"))
        graph = str(folder / Path(model_file).name)
    except Exception:
        pass
    session = onnxruntime.InferenceSession(graph, options,
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
            for i in session.get_inputs():           # decoder exports (Qwen3): an empty cache
                if i.name.startswith("past_key_values"):
                    feed[i.name] = np.zeros((len(ids), i.shape[1], 0, i.shape[3]),
                                            dtype=np.float16 if "16" in i.type else np.float32)
            if "position_ids" in names:
                feed["position_ids"] = np.clip(np.cumsum(mask, 1) - 1, 0, None)
            outputs = session.run(None, feed)
            hidden = outputs[0]
            if pooling == "sentence":                 # the export's own pooled output
                v = outputs[[o.name for o in session.get_outputs()].index("sentence_embedding")]
            elif pooling == "cls":
                v = hidden[:, 0]
            elif pooling == "last":                   # right padding: the last real token
                v = hidden[np.arange(len(ids)), mask.sum(1) - 1]
            else:
                w = mask[..., None].astype(np.float32)
                v = (hidden * w).sum(1) / np.clip(w.sum(1), 1e-9, None)
            out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.concatenate(out)
    return encode


def main() -> None:
    import psutil
    psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS if sys.platform == "win32" else 19)
    english = [s for s in json.loads(DATA.read_text(encoding="utf-8")) if s["sample_id"] in CONVS]
    ko = korean(english)
    # (turns from, questions from): "ko>en" asks in Korean about what was said in English
    sets = {"en": (english, english), "ko": (ko, ko), "ko>en": (english, ko), "en>ko": (ko, english)}
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    for repo, tok_file, model_file, q_pre, p_pre, pooling in MODELS[1 if "--e5" in sys.argv else 0:]:
        if only and only not in repo:
            continue
        encode = encoder(repo, tok_file, model_file, pooling)
        if pooling == "last":                         # Qwen3 embeds up to its end token
            p_pre, plain = "", encode
            encode = lambda texts, f=plain: f([t + "<|endoftext|>" for t in texts])  # noqa: E731
        for lang, (samples, asking) in sets.items():
            hits, total = {10: 0, 30: 0, 50: 0}, 0
            for sample, asker in zip(samples, asking):
                turns = list(turns_of(sample))
                ids = [t for t, _, _ in turns]
                docs = encode([p_pre + text for _, text, _ in turns])
                qa = [q for q in asker["qa"] if q["category"] != 5 and evidence_ids(q)]
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
