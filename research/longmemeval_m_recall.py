"""Does retrieval still find the evidence when the memory is ten times bigger?

The one claim this project makes that has never been tested is that it holds up as a memory grows.
Every number so far comes from LongMemEval_S, where a question's haystack is ~48 sessions and session
recall@16 is 98% — the index only has to discard two thirds of the store. _M asks the SAME questions
over ~475 sessions each, so k=16 there keeps 3% instead of 33%. It needs no API calls: the index is
local embeddings and the answer sessions are labelled.

What is recorded per question is the RANK of each answer session in the full ranking, not a hit at one
k, so recall at any k is a slice of the same measurement and never needs re-embedding. Results append
to recall.jsonl and the run resumes: this host has killed long jobs under memory pressure, so nothing
is held that does not have to be — instances are streamed one at a time, including _S, whose 277 MB
file costs several GB to parse whole.

    python longmemeval_m_recall.py --limit 20
    python longmemeval_m_recall.py --report      # slice what is already measured, no embedding

On a host short of memory, do a few questions per process instead — the run resumes, and the torch
peak (~2 GB of model and activations, which dwarfs everything this script itself holds) is returned
to the OS every time the process exits:

    for i in $(seq 20); do python longmemeval_m_recall.py --limit 20 --batch 1; done
"""
import argparse
import json
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from longmemeval_m_fetch import HOME, SPLIT, stream_instances

OUT = HOME / "recall"          # vector cache lives outside OneDrive: it reaches hundreds of MB
RESULTS = OUT / "recall.jsonl"
IDS = Path("runs/graph-v3.2-untouched-eval-v2/preregistration.json")
KS = (8, 16, 32, 64, 128)


def measure(row: dict, dataset: str) -> dict:
    """Where the answer sessions land in the ranking. -1 means the retriever never scored it."""
    ranked = pipe.gm_ranked_session_ids(row)
    position = {sid: i for i, sid in enumerate(ranked)}
    return {"question_id": row["question_id"], "dataset": dataset,
            "question_type": row["question_type"],
            "sessions": len(row["haystack_session_ids"]),
            "answer_ranks": [position.get(sid, -1) for sid in row["answer_session_ids"]]}


def done() -> set[tuple[str, str]]:
    if not RESULTS.exists():
        return set()
    return {(json.loads(l)["question_id"], json.loads(l)["dataset"])
            for l in RESULTS.open(encoding="utf-8")}


def report() -> dict:
    if not RESULTS.exists():
        return {}
    rows = [json.loads(l) for l in RESULTS.open(encoding="utf-8")]
    out = {}
    for dataset in ("_S", "_M"):
        mine = [r for r in rows if r["dataset"] == dataset]
        if not mine:
            continue
        out[dataset] = {
            "questions": len(mine),
            "sessions_per_question": round(sum(r["sessions"] for r in mine) / len(mine), 1),
            **{f"recall@{k}": round(sum(all(0 <= rank < k for rank in r["answer_ranks"])
                                        for r in mine) / len(mine), 4) for k in KS},
            "worst_rank_median": sorted(max(r["answer_ranks"]) for r in mine)[len(mine) // 2],
            "worst_rank": max(max(r["answer_ranks"]) for r in mine),
            # what the shipped rule would actually open at this store size, and whether it suffices
            "shipped_k": pipe.retrieval_k(round(sum(r["sessions"] for r in mine) / len(mine))),
            "recall_at_shipped_k": round(
                sum(all(0 <= rank < pipe.retrieval_k(r["sessions"]) for rank in r["answer_ranks"])
                    for r in mine) / len(mine), 4)}
    if len(out) == 2:
        common = ({r["question_id"] for r in rows if r["dataset"] == "_S"}
                  & {r["question_id"] for r in rows if r["dataset"] == "_M"})
        out["verdict"] = {"questions_on_both": len(common),
                          **{f"recall@{k}_delta": round(out["_M"][f"recall@{k}"]
                                                        - out["_S"][f"recall@{k}"], 4)
                             for k in KS}}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--batch", type=int, default=0,
                        help="questions to measure in this process (0 = all remaining)")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.report:
        result = report()
        print(json.dumps(result, indent=1))
        for dataset, row in result.items():
            if dataset.startswith("_"):
                assert row["recall_at_shipped_k"] == 1.0, (
                    f"retrieval_k() no longer covers {dataset}: "
                    f"{row['recall_at_shipped_k']:.0%} at k={row['shipped_k']}")
        return
    pipe.OUTPUT = OUT

    available = {p.stem for p in SPLIT.glob("*.json")}
    wanted = [q for q in json.loads(IDS.read_text(encoding="utf-8"))["question_ids"]
              if q in available][:args.limit]
    have = done()
    with RESULTS.open("a", encoding="utf-8") as stream:
        todo = [q for q in wanted if (q, "_M") not in have]
        if args.batch:
            todo = todo[:args.batch]
        for i, qid in enumerate(todo, 1):
            row = json.loads((SPLIT / f"{qid}.json").read_text(encoding="utf-8"))
            stream.write(json.dumps(measure(row, "_M")) + "\n")
            stream.flush()
            del row                      # ~60 MB of objects per instance; do not accumulate
            print(f"  _M {i}/{len(todo)} {qid}", flush=True)
        # _S is one 277 MB array: stream it rather than parsing it whole
        need = {q for q in wanted if (q, "_S") not in have}
        if args.batch:
            need = set(sorted(need)[:max(0, args.batch - len(todo))])
        if need:
            for row in stream_instances(pipe.DATA):
                if row["question_id"] not in need:
                    continue
                stream.write(json.dumps(measure(row, "_S")) + "\n")
                stream.flush()
                need.discard(row["question_id"])
                print(f"  _S {len(wanted) - len(need)}/{len(wanted)} {row['question_id']}",
                      flush=True)
                if not need:
                    break
    pipe._EMBEDDER and pipe._EMBEDDER.flush()
    (OUT / "report.json").write_text(json.dumps(report(), indent=1), encoding="utf-8")
    print(json.dumps(report(), indent=1))


if __name__ == "__main__":
    main()
