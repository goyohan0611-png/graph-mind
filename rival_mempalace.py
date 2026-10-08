"""Run MemPalace's retrieval through OUR reader and the official judge.

Only the retrieval differs: same questions, same reader model, same judge, same answer prompt. That
isolates the thing worth comparing — which system puts the answer in front of the model — instead of
comparing two vendors' whole stacks and two different readers.

Fairness rules followed here, and they matter more than the number:
  * MemPalace runs at its documented defaults (its own chunking, its own ChromaDB embeddings, its
    zero-LLM write path). Nothing is tuned against it.
  * Its version and settings are recorded in the report file.
  * Graph-MIND has an advantage on this question set — it was developed against dev2 — and the
    report says so.

Stage 1 mines each question's haystack into a palace (no LLM, slow on CPU), stage 2 answers.

    .venv-rivals/Scripts/python.exe rival_mempalace.py mine --limit 20      # their store, no LLM
    .venv-rivals/Scripts/python.exe rival_mempalace.py retrieve --limit 20  # their search -> spans.json
    OPENAI_API_KEY=... python rival_mempalace.py answer --limit 20          # our reader + judge
"""
from argparse import ArgumentParser
from pathlib import Path
import json
import os
import sys

OUT = Path("runs/graph-v5.0-rival-mempalace")
IDS = Path("runs/graph-v3.2-untouched-eval-v2")      # dev2 question list
DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
# RIVAL_SET=clean runs the pre-registered comparison on questions nobody was tuned on.
CLEAN = os.environ.get("RIVAL_SET") == "clean"
if CLEAN:
    IDS = Path("runs/graph-v6.0-rival-clean")
    OUT = IDS / "mempalace"
N_RESULTS = 15            # evidence spans handed to the reader (ours delivers ~15 source turns)


def questions(limit):
    """The question set, in a stable order.

    dev2 (default): sorted by id, as every earlier comparison used it. RIVAL_SET=clean: the
    pre-registered list in its FROZEN order, so a run cut short reports a prefix of that list and
    not a selection. Only the wanted instances are parsed: the dataset is 277 MB and several shards
    run at once, and parsing it whole costs each of them gigabytes.
    """
    sys.path.insert(0, ".")
    from longmemeval_m_fetch import stream_instances
    ids = json.loads((IDS / "preregistration.json").read_text(encoding="utf-8"))["question_ids"]
    # Streaming 277 MB in Python takes about a minute, which every shard would otherwise pay; the
    # few instances needed are cached once, outside the synced project folder.
    cache = Path.home() / "gm-rival-stores" / f"instances-{'clean' if CLEAN else 'dev2'}.json"
    found = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    if set(ids) - set(found):
        wanted = set(ids)
        for row in stream_instances(DATA):
            if row["question_id"] in wanted:
                found[row["question_id"]] = row
                if len(found) >= len(wanted):
                    break
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    order = ids if CLEAN else sorted(ids)
    return [found[q] for q in order if q in found][:limit]


def mine(limit):
    """One palace per question, its haystack written out as plain text files, then mined."""
    from mempalace import miner
    OUT.mkdir(parents=True, exist_ok=True)
    done = json.loads((OUT / "mined.json").read_text(encoding="utf-8")) if (OUT / "mined.json").exists() else {}
    for row in questions(limit):
        qid = row["question_id"]
        if qid in done:
            continue
        project = OUT / "corpora" / qid
        project.mkdir(parents=True, exist_ok=True)
        for date, session_id, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                           row["haystack_sessions"]):
            lines = [f"SESSION_DATE: {date}", f"SESSION_ID: {session_id}"]
            lines += [f"[{t['role']}]: {t['content']}" for t in turns]
            (project / f"{session_id}.md").write_text("\n".join(lines), encoding="utf-8")
        palace = OUT / "palaces" / qid
        palace.mkdir(parents=True, exist_ok=True)
        miner.mine(str(project), str(palace), agent="benchmark")
        done[qid] = True
        (OUT / "mined.json").write_text(json.dumps(done), encoding="utf-8")
        print("mined", qid, flush=True)
    print("palaces:", len(done))


def retrieve(qid, question):
    from mempalace import searcher
    result = searcher.search_memories(question, str(OUT / "palaces" / qid), n_results=N_RESULTS)
    rows = result.get("results") or result.get("memories") or []
    spans = []
    for row in rows:
        text = row.get("content") or row.get("text") or row.get("document") or ""
        if text:
            spans.append({"session_id": row.get("source_file", ""), "text": text[:1000]})
    return spans


def retrieve_all(limit):
    """Stage 2, in the rivals venv: dump MemPalace's spans so the answer stage needs no mempalace."""
    OUT.mkdir(parents=True, exist_ok=True)
    spans = {}
    for row in questions(limit):
        spans[row["question_id"]] = retrieve(row["question_id"], row["question"])
        print("retrieved", row["question_id"], len(spans[row["question_id"]]), flush=True)
    (OUT / "spans.json").write_text(json.dumps(spans, ensure_ascii=False), encoding="utf-8")
    print("spans written:", len(spans))


def answer(limit):
    sys.path.insert(0, ".")
    import longmemeval_retrieval_executor as pipe
    import official_judge_v073 as judge
    key = os.environ["OPENAI_API_KEY"]
    rows = questions(limit)
    public = [{k: r[k] for k in ("question_id", "question_type", "question", "question_date",
                                 "answer", "answer_session_ids")} for r in rows]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "questions.json").write_text(json.dumps(public, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    path = OUT / "answers.jsonl"
    previous = pipe.load_jsonl(path, "question_id")
    pending = [q for q in public if q["question_id"] not in pipe.finished(previous)]

    spans_by_question = json.loads((OUT / "spans.json").read_text(encoding="utf-8"))

    def send(question):
        spans = spans_by_question[question["question_id"]]
        body = pipe.answer_body(question, {}, [], None, None, spans)   # same prompt, same reader
        return pipe.safe_call(key, body, {"question_id": question["question_id"],
                                          "spans_supplied": len(spans)},
                              lambda raw: json.loads(pipe.response_text(raw)))
    pipe.run_parallel(pending, path, 4, send)
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    report = judge.run()
    print(json.dumps(report["by_type"], indent=1))


def main():
    parser = ArgumentParser()
    parser.add_argument("stage", choices=["mine", "retrieve", "answer"])
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    {"mine": mine, "retrieve": retrieve_all, "answer": answer}[args.stage](args.limit)


if __name__ == "__main__":
    main()
