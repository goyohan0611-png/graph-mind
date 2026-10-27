"""LongMemEval_S candidate pools in locomo_pools.py's shape, to check that a re-ranker learned on
LoCoMo does not hurt LongMemEval (learning_curve.py --transfer).

Same store and searches as bench/evidence.py (every turn verbatim, embedded, then the shipped word
and meaning searches, top 50 each). Only the pooled turns' texts are kept, with their position
in the haystack.

    python research/lme_pools.py            # all 500 (abstention questions have no evidence)
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "bench"))

from evidence import CACHE, DATA, fill  # noqa: E402
from locomo_pools import POOL, hits  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

from graph_mind.conversation_memory import ConversationMemoryStore  # noqa: E402
from graph_mind.local_embedder import LOCAL_MODEL, LocalEmbedder  # noqa: E402
from graph_mind.mcp_server import _PIECES, CONTEXT_LIMIT, excerpt, fuse, semantic_turns  # noqa: E402
from graph_mind.warmup import warm_turns  # noqa: E402

OUT = ROOT / "research/runs/graph-v9.20-learning/lme_pools.json"


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="first N questions only")
    args = parser.parse_args()
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(CACHE / f"embed-{slug}.json", model=LOCAL_MODEL)
    result = {"turns": {}, "questions": []}
    with tempfile.TemporaryDirectory() as directory:
        for seen, row in enumerate(stream_instances(DATA)):
            if args.limit and seen >= args.limit:
                break
            qid = row["question_id"]
            store = Path(directory) / f"{qid}.sqlite"
            wanted = fill(store, row)
            if not wanted:
                store.unlink()
                continue
            while warm_turns(store, limit=2048, embedder=embedder):
                pass
            query = row["question"]
            with ConversationMemoryStore(store) as conversations:
                words = [excerpt(t) for t in conversations.search(query, limit=POOL)["turns"]]
                order = [r[0] for r in conversations.db.execute(
                    "SELECT turn_id FROM conversation_turns ORDER BY happened_at, source_ordinal")]
            meaning = semantic_turns(store, query, limit=POOL, embedder=embedder)
            shipped = fuse(meaning, words, limit=CONTEXT_LIMIT)
            where = {tid: i / max(len(order) - 1, 1) for i, tid in enumerate(order)}
            pooled = {t["turn_id"]: t.get("full", t["content"]) for t in meaning + words}
            result["turns"][qid] = {tid: {"text": text, "pos": where.get(tid, 0.0)}
                                    for tid, text in pooled.items()}
            result["questions"].append({
                "conv": qid, "n": seen, "question": query, "answer": str(row["answer"]),
                "category": row["question_type"], "evidence": sorted(wanted),
                "meaning": [[t["turn_id"], t["similarity"]] for t in meaning],
                "words": [t["turn_id"] for t in words],
                "shipped": [t["turn_id"] for t in shipped]})
            _PIECES.pop(str(store), None)
            store.unlink()
            print(f"\r  {seen + 1} questions", end="", flush=True)
    embedder.cache.flush()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print("\nall", hits(result["questions"]))


if __name__ == "__main__":
    main()
