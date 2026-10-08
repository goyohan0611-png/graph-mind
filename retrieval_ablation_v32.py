"""v3.2 retrieval ablation (offline, no API): session recall@k on the 120 dev questions.

A  current: best 500-char chunk of the first 3000 chars of each serialized session
U  user turns only, full length (500-char windows); assistant turns added for assistant-history Qs
B  BM25 over the same user-turn windows (stdlib implementation)
UB U + B fused per session with reciprocal-rank fusion

    python retrieval_ablation_v32.py
"""
from collections import Counter
from pathlib import Path
import json
import math
import re
import shutil

import longmemeval_retrieval_executor as pipe
import untouched_eval_v1 as v1
from longmemeval_adapter import load_instances
from semantic_grounding_v073 import LocalEmbedder, cosine

OUT = Path("runs/graph-v3.2-retrieval")
K = 8
_STOP = set("a an the i my me you your to of in on for and or is was were did do does have has had "
            "what how many much when which who where with at by from that this it be am are".split())


def _stem(t):  # ponytail: crude suffix strip (graduated/graduate), swap for Snowball if needed
    for suf in ("ing", "ed", "es", "s", "e"):
        if len(t) > 4 and t.endswith(suf):
            return t[:-len(suf)]
    return t


def tokens(text):
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


def bm25_scores(query, docs, k1=1.5, b=0.75):
    toks = [tokens(d) for d in docs]
    avg = sum(map(len, toks)) / max(1, len(toks))
    df = Counter(t for ts in toks for t in set(ts))
    n, q = len(docs), set(tokens(query))
    out = []
    for ts in toks:
        tf = Counter(ts)
        out.append(sum(math.log(1 + (n - df[t] + .5) / (df[t] + .5)) * tf[t] * (k1 + 1)
                       / (tf[t] + k1 * (1 - b + b * len(ts) / avg)) for t in q if t in tf))
    return out


def windows(text, size=500):
    return [text[i:i + size] for i in range(0, len(text), size) if text[i:i + size].strip()]


def pieces(row, user_only):
    assistant = pipe.asks_assistant_history(row["question"])
    out = []
    for sid, date, turns in zip(row["haystack_session_ids"], row["haystack_dates"],
                                row["haystack_sessions"]):
        if not user_only:
            out += [(sid, w) for w in windows(pipe.serialize_session(date, sid, turns)[:3000])]
            continue
        for t in turns:
            if t["role"] == "user" or assistant:
                out += [(sid, w) for w in windows(t["content"])]
    return out


def ranking(owner, scores):
    best = {}
    for sid, s in zip(owner, scores):
        best[sid] = max(best.get(sid, -1e9), s)
    return sorted(best, key=best.get, reverse=True)


def rrf(*rankings, c=60):
    score = Counter()
    for r in rankings:
        for i, sid in enumerate(r):
            score[sid] += 1 / (c + i + 1)
    return [sid for sid, _ in score.most_common()]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / "embed-cache-local.json"
    if not cache.exists():
        shutil.copyfile(Path("runs/graph-v3.1-dev120/embed-cache-local.json"), cache)
    v1._memory_only_cache()
    emb = LocalEmbedder(cache)
    ids = set(json.loads((v1.OUT / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    rows = [r for r in load_instances(pipe.DATA) if r["question_id"] in ids]
    hits = {m: [0, 0, 0] for m in ("A", "U", "B", "UB")}  # all-found Qs, found sessions, total
    per_q = []
    for n, row in enumerate(rows, 1):
        qv = emb.embed([row["question"]])[0]
        need = set(row["answer_session_ids"])
        a = pieces(row, user_only=False)
        u = pieces(row, user_only=True)
        rank = {"A": ranking([s for s, _ in a], [cosine(qv, v) for v in emb.embed([t for _, t in a])])}
        rank["U"] = ranking([s for s, _ in u], [cosine(qv, v) for v in emb.embed([t for _, t in u])])
        rank["B"] = ranking([s for s, _ in u], bm25_scores(row["question"], [t for _, t in u]))
        rank["UB"] = rrf(rank["U"], rank["B"])
        rec = {"question_id": row["question_id"], "type": row["question_type"]}
        for m, r in rank.items():
            got = need & set(r[:K])
            hits[m][0] += got == need; hits[m][1] += len(got); hits[m][2] += len(need)
            rec[m] = sorted(got)
        per_q.append(rec)
        if n % 20 == 0:
            print(n, {m: h[0] for m, h in hits.items()}, flush=True)
            emb.cache_path.write_text(json.dumps(emb.cache), encoding="utf-8")
    emb.cache_path.write_text(json.dumps(emb.cache), encoding="utf-8")
    report = {m: {"all_answer_sessions_in_top8": f"{h[0]}/{len(rows)}",
                  "session_recall": round(h[1] / h[2], 4)} for m, h in hits.items()}
    (OUT / "recall.json").write_text(json.dumps({"k": K, "report": report, "per_question": per_q},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
