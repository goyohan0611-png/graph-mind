"""Run Mem0's retrieval through OUR reader and the official judge.

Mem0 is the rival that matters most, because it is the only one in the same architecture class: like
Graph-MIND it spends an LLM at WRITE time to turn conversations into facts. MemPalace does not (its
extraction is English keyword regexes), so beating it says nothing about whose extraction is better.
This comparison does.

Only the retrieval differs: same questions, same answer prompt, same reader, same official judge.

Fairness rules, which matter more than the number:
  * Mem0 runs at its documented defaults. Nothing is tuned against it, and its version, model and
    vector store are recorded in the report.
  * Its write-side token usage is MEASURED, not inferred. Mem0 pays a write bill like ours, so for
    once the cost comparison is like for like — and reading its code to count LLM calls is not the
    same as counting tokens, so every OpenAI call it makes is intercepted and its usage summed.
  * Graph-MIND has an advantage on the dev2 question list \u2014 it was developed against it. Questions
    outside that list are preferred where the budget allows, and the report says which were used.

One question per process, in a loop. Mem0 holds a file lock on a shared qdrant under ~/.mem0, so a
second Memory() in the same process dies with "already accessed by another instance"; letting the
process exit releases it, and it bounds memory on a host that reaps long jobs. Each question takes
~18 minutes (Mem0 runs an LLM per session, and its default infer=True also asks the LLM to reconcile
each new fact against the store, so its write cost grows as the store fills).

    for i in $(seq 20); do .venv .../mem0/Scripts/python.exe rival_mem0.py add --batch 1; done

Questions are independent of each other (separate stores), so several can run at once with --shard,
which is how a batch fits inside a bounded run: three processes, three questions, still ~7 minutes.
Completion is recorded as one marker file per question rather than a shared JSON map, so concurrent
writers cannot drop each other's entries. Mem0 also keeps a process-wide qdrant under ~/.mem0 that
only one process may open, so each shard needs its own MEM0_DIR.

    for i in 0 1 2; do MEM0_DIR=~/.mem0-$i ... rival_mem0.py add --batch 1 --shard $i/3 & done; wait
    .venv .../mem0/Scripts/python.exe rival_mem0.py retrieve --limit 20  # their search -> spans.json
    python rival_mem0.py answer --limit 20                               # our reader + judge
"""
from argparse import ArgumentParser
from collections import Counter
from pathlib import Path
import json
import os
import shutil
import sys

OUT = Path("runs/graph-v5.9-rival-mem0")
# Their vector stores are ~10 MB per question and are rebuildable, so they live outside the
# OneDrive-synced project folder; the evidence that matters (spans, answers, judge) stays in OUT.
STORES = Path.home() / "gm-rival-stores" / "mem0"
IDS = Path("runs/graph-v3.2-untouched-eval-v2")      # same dev2 list the MemPalace run used
# RIVAL_SET=clean runs the pre-registered comparison on questions nobody was tuned on.
CLEAN = os.environ.get("RIVAL_SET") == "clean"
if CLEAN:
    IDS = Path("runs/graph-v6.0-rival-clean")
    OUT = IDS / "mem0"
    STORES = Path.home() / "gm-rival-stores" / "mem0-clean"
N_RESULTS = 15            # evidence spans handed to the reader, as in rival_mempalace.py
DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")


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


def instrument() -> Counter:
    """Sum every OpenAI token Mem0 spends, by wrapping the two endpoints it calls.

    Mem0 exposes no usage accounting, and counting its LLM calls by reading its code says nothing
    about their size. This patches `create` on the chat-completions and embeddings resources, so the
    number reported is what OpenAI billed, not an estimate.
    """
    from openai.resources import embeddings
    from openai.resources.chat import completions

    totals = Counter()

    def wrap(resource, kind):
        original = resource.create

        def create(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            usage = getattr(result, "usage", None)
            if usage is not None:
                totals[f"{kind}_calls"] += 1
                totals[f"{kind}_input"] += getattr(usage, "prompt_tokens", 0) or 0
                totals[f"{kind}_output"] += getattr(usage, "completion_tokens", 0) or 0
            return result

        resource.create = create

    wrap(completions.Completions, "llm")
    wrap(embeddings.Embeddings, "embed")
    return totals


def store_for(qid: str):
    """One Mem0 store per question, on disk, so a question's haystack cannot leak into another's."""
    from mem0 import Memory
    home = STORES / qid
    home.mkdir(parents=True, exist_ok=True)
    return Memory.from_config({
        "vector_store": {"provider": "qdrant",
                         "config": {"path": str(home / "qdrant"), "on_disk": True,
                                    "collection_name": "longmemeval"}},
        # Mem0 2.2.1 does not run at its own defaults on OpenAI: its default model is gpt-5-mini, but
        # its reasoning-model list holds "gpt-5", "gpt-5o", "gpt-5o-mini" and "gpt-5o-micro" and not
        # "gpt-5-mini", while the prefix rule deliberately skips gpt-5.x — so it sends
        # temperature=0.1 and OpenAI rejects it. Its defaults are internally inconsistent and one of
        # the two has to give. The rest of its LLM config — temperature 0.1, top_p 0.1,
        # max_tokens 2000 — is written for a model that ACCEPTS those parameters, i.e. not a
        # reasoning model, so honouring the parameters and naming a model that takes them stays
        # closer to the documented intent than forcing the newer model into reasoning mode. It is
        # also Mem0's own historical default. Everything else, including every prompt, is untouched.
        "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
        "version": "v1.1"})


def finished() -> set:
    """One marker file per completed question: safe for several processes at once."""
    marks = OUT / "done"
    return {p.stem for p in marks.glob("*.mark")} if marks.exists() else set()


def add(limit, batch=0, shard="0/1"):
    """Write each haystack into Mem0 the way its README does: one add() per session's messages."""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "done").mkdir(exist_ok=True)
    index, shards = (int(x) for x in shard.split("/"))
    done = finished()
    written = 0
    totals = instrument()
    for position, row in enumerate(questions(limit)):
        qid = row["question_id"]
        if qid in done or position % shards != index:
            continue
        if batch and written >= batch:
            break
        # A question is marked done only once every session is in, so a store left behind by an
        # interrupted run is partial. Rebuild it from empty rather than adding on top, which would
        # duplicate memories and quietly flatter the retrieval.
        shutil.rmtree(STORES / qid, ignore_errors=True)
        memory = store_for(qid)
        for date, session_id, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                           row["haystack_sessions"]):
            messages = [{"role": t["role"], "content": t["content"]} for t in turns]
            memory.add(messages, user_id=qid, metadata={"session_id": session_id, "date": date})
        written += 1
        (OUT / "done" / f"{qid}.mark").write_text(str(len(row["haystack_session_ids"])),
                                                 encoding="utf-8")
        with (OUT / "write-usage.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"question_id": qid,
                                     "sessions": len(row["haystack_session_ids"]),
                                     **totals}) + "\n")
        totals.clear()
        print("added", qid, len(row["haystack_session_ids"]), "sessions", flush=True)
    print("stores:", len(finished()))


def retrieve_all(limit, batch=0, shard="0/1"):
    """Dump Mem0's spans, one file per question, so the answer stage never needs mem0 installed.

    Same constraint as add(): a second Memory() in one process trips Mem0's process-wide qdrant lock,
    so this also runs one question per process (--batch 1) and in shards, and merges at the end.
    """
    folder = OUT / "spans"
    folder.mkdir(parents=True, exist_ok=True)
    index, shards = (int(x) for x in shard.split("/"))
    taken = 0
    for position, row in enumerate(questions(limit)):
        qid = row["question_id"]
        target = folder / f"{qid}.json"
        if target.exists() or position % shards != index:
            continue
        if batch and taken >= batch:
            break
        # Mem0 2.2.1's documented call: the entity goes in `filters`, the count is `top_k`.
        # threshold (0.1) and rerank (off) stay at its defaults.
        result = store_for(qid).search(row["question"], filters={"user_id": qid}, top_k=N_RESULTS)
        found = result.get("results", result) if isinstance(result, dict) else result
        spans = [{"session_id": (r.get("metadata") or {}).get("session_id", ""),
                  "text": str(r.get("memory") or r.get("text") or "")[:1000]}
                 for r in found if (r.get("memory") or r.get("text"))]
        target.write_text(json.dumps(spans, ensure_ascii=False), encoding="utf-8")
        taken += 1
        print("retrieved", qid, len(spans), flush=True)
    merged = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")}
    (OUT / "spans.json").write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    print("spans merged:", len(merged))


def answer(limit):
    sys.path.insert(0, ".")
    import longmemeval_retrieval_executor as pipe
    import official_judge_v073 as judge
    key = os.environ["OPENAI_API_KEY"]
    public = [{k: r[k] for k in ("question_id", "question_type", "question", "question_date",
                                 "answer", "answer_session_ids")} for r in questions(limit)]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "questions.json").write_text(json.dumps(public, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    path = OUT / "answers.jsonl"
    previous = pipe.load_jsonl(path, "question_id")
    spans_by_question = json.loads((OUT / "spans.json").read_text(encoding="utf-8"))

    def send(question):
        spans = spans_by_question[question["question_id"]]
        body = pipe.answer_body(question, {}, [], None, None, spans)   # same prompt, same reader
        return pipe.safe_call(key, body, {"question_id": question["question_id"],
                                          "spans_supplied": len(spans)},
                              lambda raw: json.loads(pipe.response_text(raw)))
    done = pipe.finished(previous)                    # failed rows are retried
    pipe.run_parallel([q for q in public if q["question_id"] not in done], path, 4, send)
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    report = judge.run()
    print(json.dumps(report["by_type"], indent=1))


def main():
    parser = ArgumentParser()
    parser.add_argument("stage", choices=["add", "retrieve", "answer"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--shard", default="0/1", help="i/n: take every n-th question, offset i")
    parser.add_argument("--batch", type=int, default=0,
                        help="questions to write in this process (0 = all; see the note above)")
    args = parser.parse_args()
    if args.stage == "add":
        add(args.limit, args.batch, args.shard)
    else:
        if args.stage == "retrieve":
            retrieve_all(args.limit, args.batch, args.shard)
        else:
            answer(args.limit)


if __name__ == "__main__":
    main()
