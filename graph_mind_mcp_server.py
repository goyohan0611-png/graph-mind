"""Model-independent MCP interface for Graph-MIND development memory."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
import os
import sys
import threading
import uuid

from mcp import types
from mcp.server import MCPServer

from associative_memory import AssociativeMemoryIndex
from coding_memory import CodingMemoryStore
from conversation_memory import ConversationMemoryStore
from development_memory import DevelopmentMemoryStore
from development_paths import default_development_db
from local_brain import LocalBrainStore
from universal_personal_memory import build_context_pack, decide_recall


if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


SERVER_VERSION = "0.5.0"
RecordableEventType = Literal[
    "OBJECTIVE_SET", "TASK_STARTED", "TASK_COMPLETED", "TASK_BLOCKED",
    "TASK_CANCELLED", "FILE_CHANGED", "DECISION_RECORDED", "TEST_RECORDED",
    "NEXT_ACTION_SET", "NEXT_ACTION_COMPLETED", "SESSION_SUMMARY",
    "EXPERIMENT_STARTED", "EXPERIMENT_STOPPED",
]


# The embedding model is loaded from the local Hugging Face cache, but the library still asks the
# hub over the network whether a newer revision exists, every time it loads. That made the first
# save of a session take ~95 s and sent requests off the machine for no reason. Once the model is
# on disk, stay offline.
if (Path.home() / ".cache" / "huggingface" / "hub"
        / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2").is_dir():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


_index_lock = threading.Lock()
_index_due = threading.Event()


def index_in_background(database):
    """Refresh the associative index and the vectors without making the caller wait.

    A save is complete the moment its row is written: recall reads the memory table directly.
    Only associative search needs the index and the vectors, so they are brought up to date by one
    background worker; saves that arrive while it runs are folded into its next pass.
    """
    _index_due.set()
    if not _index_lock.acquire(blocking=False):
        return                                  # the running worker will see _index_due

    def work():
        try:
            while _index_due.is_set():
                _index_due.clear()
                try:
                    with AssociativeMemoryIndex(database) as index:
                        index.sync_sources(limit=200)
                    from embedding_warmup import warm
                    warm(database, limit=200)
                except Exception:               # never lose a save over an index refresh;
                    pass                        # brain_index reports and repairs the backlog
        finally:
            _index_lock.release()
            if _index_due.is_set():             # a save landed between the last pass and release
                index_in_background(database)

    threading.Thread(target=work, name="graph-mind-index", daemon=True).start()


def wait_for_index(timeout=120.0):
    """Block until background indexing has finished (tests, and shutting down cleanly)."""
    import time
    deadline = time.monotonic() + timeout
    while _index_due.is_set() or _index_lock.locked():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def local_time(value):
    """The store keeps naive local times. Models send "2026-10-01T18:25:00+09:00" or just
    "2026-10-01"; both were rejected outright (CLOCK_DOMAIN_MISMATCH, TIMESTAMP_REQUIRED), so the
    memory was lost. Convert to this machine's local clock instead."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if "T" not in text:
        text += "T00:00:00"
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.isoformat(timespec="microseconds")


def configured_db_path():
    """This machine's index. It is never in the shared folder: a SQLite file there would be
    corrupted by the sync client. The shared memory itself is the log (brain_log.py)."""
    configured = os.environ.get("GRAPH_MIND_DB")
    return Path(configured).expanduser() if configured else default_development_db()


def shared_sync(database):
    """Pull every device's new log lines into this index before answering. A no-op without a
    shared folder, and cheap when nothing changed (a size check per log file)."""
    from brain_log import folder, sync
    try:
        shared = folder()
        return sync(shared, database) if shared else None
    except Exception as error:      # the hub PC is off, or the folder is offline: answer from
        return {"error": type(error).__name__}   # this PC's index; the rest arrives next time


# The calling model already judged that this request may need memory: that is why it called the
# tool. The keyword router second-guessed it and skipped 38 of 120 LongMemEval questions outright
# (product_recall_eval.py: evidence reaching the packet 48.5% -> 66.7% without it), and deciding
# what a question is about is a language judgement this engine leaves to the model anyway.
DEFAULT_CONTEXT_POLICY = "always"


# Retrieval over conversation turns, ported from the benchmark pipeline where it was measured:
# the index is the USER's turns cut into 500-character pieces (the same slices, so the measured
# choices carry over), compared by meaning; assistant turns, which an index of user turns cannot
# see, are reached by the existing word search; the two rankings are fused. Each hit goes to the
# model as the matching 500-character piece rather than the whole turn: long assistant turns used
# to fill the 6,000-character packet after four or five items.
PIECE = 500
INLINE_EMBED_LIMIT = 64
ASSISTANT_TURN_CAP = 3000
WINDOW_SLACK = 7           # days either side of a resolved "around" date
_EMBEDDER = {}
_backfill = threading.Lock()


def embed_in_background(embedder, texts):
    """Embed a backlog once, in one background worker, then persist it."""
    if not _backfill.acquire(blocking=False):
        return                                   # already filling; the next search retries

    def work():
        try:
            for start in range(0, len(texts), 256):
                embedder.embed(texts[start:start + 256])
            embedder.flush()
        except Exception:
            pass
        finally:
            _backfill.release()

    threading.Thread(target=work, name="graph-mind-backfill", daemon=True).start()


def embedder_for(database):
    """One on-device embedder per store, kept loaded for the life of the server."""
    key = str(database)
    if key not in _EMBEDDER:
        from semantic_recall import default_embedder
        _EMBEDDER[key] = default_embedder(database)
    return _EMBEDDER[key]


def semantic_turns(database, query, *, scopes=None, since=None, until=None, limit=20, embedder,
                   about_assistant=False):
    """Turns ranked by meaning; each carries the 500-character piece that matched.

    Only the user's turns by default: indexing the assistant's long prose too buried the user's
    facts (benchmark: answer sessions in the top 8 went 105 -> 115 of 120 when it was left out).
    When the question is about what the assistant itself said, its turns join the index."""
    with ConversationMemoryStore(database) as store:
        where, parameters = ["role IN ('user','assistant')" if about_assistant
                             else "role='user'"], []
        if scopes:
            where.append("scope IN (" + ",".join("?" for _ in scopes) + ")")
            parameters.extend(scopes)
        if since:
            where.append("happened_at>=?")
            parameters.append(since)
        if until:
            where.append("happened_at<?")
            parameters.append(until)
        turns = [store._turn(row) for row in store.db.execute(
            "SELECT * FROM conversation_turns WHERE " + " AND ".join(where), parameters)]
    from embedding_warmup import turn_pieces
    owners, pieces = [], []
    for turn in turns:
        for start, piece in turn_pieces(turn["content"]):
            owners.append((turn, start))
            pieces.append(piece)
    if not pieces:
        return []
    if hasattr(embedder.cache, "refresh"):
        embedder.cache.refresh()        # vectors the capture service embedded since we loaded
    # Never make a question wait for the store to be embedded: a store that has never been
    # searched by meaning holds ~1,500 pieces per thousand turns, minutes on a busy laptop. A few
    # new pieces are embedded inline; a backlog is filled in the background while this question
    # is answered from what is already embedded (word search covers the rest meanwhile).
    missing = [p for p in pieces if embedder._key(p.strip()[:2000]) not in embedder.cache]
    if len(missing) > INLINE_EMBED_LIMIT:
        embed_in_background(embedder, missing)
        keep = [i for i, p in enumerate(pieces)
                if embedder._key(p.strip()[:2000]) in embedder.cache]
        owners, pieces = [owners[i] for i in keep], [pieces[i] for i in keep]
        if not pieces:
            return []
    import numpy as np
    question = np.asarray(embedder.embed([query])[0], dtype=np.float64)
    # One matrix product instead of a Python loop per piece: 5 s -> 0.05 s at 40,000 pieces, which
    # a few months of captured chat reaches. Same cosine, in float64 as before.
    matrix = np.stack([np.asarray(v, dtype=np.float64) for v in embedder.embed(pieces)])
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(question)
    scores = np.divide(matrix @ question, norms, out=np.zeros(len(pieces)), where=norms > 0)
    best = {}
    # ponytail: a full scan of every piece per query; fine for one person's years of chat, an
    # approximate-nearest-neighbour index is the upgrade if a store reaches millions of pieces.
    for (turn, start), piece, score in zip(owners, pieces, scores.tolist()):
        if score > best.get(turn["turn_id"], (-2.0,))[0]:
            best[turn["turn_id"]] = (score, turn, start, piece)
    ranked = sorted(best.values(), key=lambda hit: hit[0], reverse=True)[:limit]
    return [{**turn, "content": piece, "span": [start, start + len(piece)], "full": turn["content"],
             "similarity": round(score, 4)} for score, turn, start, piece in ranked]


def fuse(*rankings, limit):
    """Reciprocal-rank fusion: a turn high in both lists beats one high in only one."""
    scores, keep = {}, {}
    for ranking in rankings:
        for rank, turn in enumerate(ranking):
            scores[turn["turn_id"]] = scores.get(turn["turn_id"], 0.0) + 1.0 / (60 + rank)
            keep.setdefault(turn["turn_id"], turn)       # the first list's version (its excerpt)
    order = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [keep[turn_id] for turn_id in order]


def excerpt(turn):
    """A word-search hit, cut to one piece: the answering sentence sits within the first 500
    characters of every answer-bearing turn in LongMemEval (span_cap_check.py)."""
    if len(turn["content"]) <= PIECE:
        return turn
    return {**turn, "content": turn["content"][:PIECE], "span": [0, PIECE],
            "full": turn["content"]}


def window(around):
    """"2023-05-14" or "2023-05-01..2023-05-31" -> [start, end) padded by WINDOW_SLACK days:
    "two weeks ago" is approximate, and the turn that mentions it is rarely on the exact day."""
    if not around:
        return None
    first, _, last = str(around).partition("..")
    try:
        start = datetime.fromisoformat(local_time(first.strip()))
        end = datetime.fromisoformat(local_time((last or first).strip()))
    except ValueError:      # "last week" sent unresolved: a hint, so ignore it rather than fail
        return None
    return ((start - timedelta(days=WINDOW_SLACK)).isoformat(timespec="microseconds"),
            (end + timedelta(days=WINDOW_SLACK + 1)).isoformat(timespec="microseconds"))


def gather(database, query, *, scopes=None, as_of, knowledge_cutoff=None, limit=8,
           recent=False, since=None, embedder=None, about_assistant=False, around=None):
    """Curated memories and conversation turns for one request, as recall and context share it."""
    shared_sync(database)
    since = local_time(since) if since else None
    span = window(around)
    with LocalBrainStore(database) as store:
        result = store.recall(query, scopes=scopes, as_of=as_of, knowledge_cutoff=knowledge_cutoff,
                              limit=limit, recent=recent, since=since)
    with ConversationMemoryStore(database) as conversations:
        if recent:
            names = sorted({name for match in result["matches"] for name in match["named"]})
            if names:      # "what did Kongi do lately": turns about Kongi, newest first
                hits = conversations.search(" ".join(names), scopes=scopes, limit=50)["turns"]
                turns = sorted((t for t in hits if not since or t["happened_at"] >= since),
                               key=lambda t: t["happened_at"], reverse=True)[:limit]
            else:          # "what was I doing": simply the latest turns
                turns = conversations.latest(scopes=scopes, since=since, limit=limit)["turns"]
        else:
            words = [excerpt(t) for t in conversations.search(
                         query, scopes=scopes, limit=max(limit, 20))["turns"]
                     if not since or t["happened_at"] >= since]
    if not recent:
        try:
            meaning = semantic_turns(database, query, scopes=scopes, since=since,
                                     limit=max(limit, 20),
                                     embedder=embedder or embedder_for(database),
                                     about_assistant=about_assistant)
        except Exception:                 # no local model yet: word search still answers
            meaning = []
        rankings = [meaning, words]
        if span:
            # "What did I buy four weeks ago?": the same search inside that window ranks first.
            # A boost, not a filter: a wrong or loose date still leaves the overall ranking in.
            try:
                inside = semantic_turns(database, query, scopes=scopes, since=span[0],
                                        until=span[1], limit=max(limit, 20),
                                        embedder=embedder or embedder_for(database),
                                        about_assistant=about_assistant)
            except Exception:
                inside = []
            rankings = [inside, [t for t in words if span[0] <= t["happened_at"] < span[1]],
                        *rankings]
        turns = fuse(*rankings, limit=limit)
        if about_assistant:
            # "How many mummies were in the one-shot you wrote?" needs the whole answer, not a
            # 500-character piece of it: the assistant's answers run 1,000-4,000 characters and
            # counts and list positions span them. (Users state facts early in a turn; assistants
            # do not.) Their own answers go in whole, up to ASSISTANT_TURN_CAP.
            turns = [{**t, "content": t.get("full", t["content"])[:ASSISTANT_TURN_CAP],
                      "span": [0, min(len(t.get("full", t["content"])), ASSISTANT_TURN_CAP)]}
                     if t["role"] == "assistant" else t for t in turns]
    turns = [{k: v for k, v in t.items() if k != "full"} for t in turns]
    result["conversation_turns"] = turns
    if result["status"] == "UNKNOWN" and turns:
        result["status"] = "KNOWN"
        result["reason"] = "CONVERSATION_TURNS_FOUND"
    return result


def build_server(db_path=None):
    database = Path(db_path).expanduser() if db_path is not None else configured_db_path()
    server = MCPServer(
        "graph-mind-memory",
        title="Graph-MIND Universal Personal Memory",
        description="User-owned local brain shared across models, sessions and applications.",
        instructions=(
            "Remember proactively. Whenever the user mentions something about their life, work, "
            "plans, preferences, people, places, meals, purchases, health, decisions or events, "
            "call brain_remember right away with their own words, without asking first and "
            "without waiting to be told to remember it. Saving too much costs nothing; forgetting "
            "is what the user notices. Do it silently: never tell the user that you saved, "
            "stored or noted something, and do not add lines like 'I saved that to memory'. "
            "Mention memory only when the user asks what you remember or whether it was saved. "
            "Call brain_context first when a request may depend on the user's prior conversations, "
            "facts, preferences, documents, projects or work. It skips unrelated requests and "
            "returns a bounded evidence packet instead of replaying full history. Use brain_recall "
            "for direct memory inspection. It searches curated memories and automatically captured "
            "conversation turns without requiring a project ID. Use brain_associate for vague cues "
            "or indirect reminders, then verify its candidate sources with the deterministic tools. "
            "For questions about code changed in a time range, project, file or symbol, translate "
            "the wording into code_activity filters and use its indexed evidence. "
            "Call project_status or resume_project when an exact project ID is already known. "
            "Treat scope_completeness=UNATTESTED as partial observed history. "
            "Use brain_remember for general sourced memories and memory_record for sourced "
            "development events. Preserve provenance."
        ),
        version=SERVER_VERSION,
    )

    @server.tool(
        name="brain_remember",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def brain_remember(content: str, title: str | None = None, memory_type: str = "fact",
                       source_ref: str = "conversation", scope: str = "global",
                       tags: list[str] | None = None,
                       entities: list[str] | None = None,
                       effective_at: str | None = None,
                       source_type: str = "mcp-client",
                       supersedes_memory_id: str | None = None,
                       memory_id: str | None = None,
                       memory_class: str | None = None,
                       modality: str | None = None,
                       retention: str | None = None,
                       importance: float | None = None,
                       confidence: float | None = None,
                       attributes: dict[str, Any] | None = None,
                       related_memory_ids: list[str] | None = None) -> dict[str, Any]:
        """Save what the user just told you, in their own words. Call this proactively.

        Use it the moment the user mentions anything about their life, work, plans, preferences,
        people, places, meals, purchases, health, decisions or events, e.g. "I had kimchi fried
        rice today", "my dog is called Kongi", "we moved the meeting to Tuesday". Do not ask
        whether to save it and do not wait to be told "remember this". Only `content` is
        required: put the user's own sentence there verbatim. Add `entities` (who or what it is
        about) and `effective_at` (when it happened, ISO time) when you know them. If it updates
        something remembered earlier, pass that memory's id as `supersedes_memory_id`.

        Save silently. Do not tell the user you saved it ("I've noted that", "저장해 뒀어"); just
        carry on with the conversation. Talk about memory only if the user asks what you remember
        or whether something was saved.
        """
        # Saving is proactive, so a key the user pastes can arrive here too: mask it with the same
        # rules as captured conversations before it reaches the store or the shared log.
        from conversation_memory import _redact
        content = _redact(content)[0]
        title = _redact(title)[0] if title else content.strip().splitlines()[0][:80]
        known_at = _now()
        memory = {"memory_id": memory_id or "brain-" + uuid.uuid4().hex,
            "scope": scope, "memory_type": memory_type, "title": title,
            "content": content, "effective_at": local_time(effective_at) or known_at,
            "known_at": known_at, "actor": "assistant", "tags": tags or [],
            "entities": entities or [],
            "provenance": {"source_type": source_type, "source_ref": source_ref},
            "supersedes_memory_id": supersedes_memory_id}
        optional_profile = {"memory_class": memory_class, "modality": modality,
            "retention": retention, "importance": importance, "confidence": confidence,
            "attributes": attributes, "related_memory_ids": related_memory_ids}
        memory.update({key: value for key, value in optional_profile.items()
                       if value is not None})
        shared_sync(database)                      # a memory being superseded may come from another PC
        with LocalBrainStore(database) as store:
            result = store.remember(memory)
        from brain_log import append, folder
        if result.get("disposition") != "ALREADY_RECORDED":
            try:
                shared = folder()
                if shared:
                    append(shared, "memory", memory)    # now every device's index imports it
            except Exception:
                # ponytail: saved on this PC only; brain_folder(share_existing=true) sends it later
                pass
        index_in_background(database)
        return {**result, "scope": scope, "store": str(database), "indexing": "background"}

    @server.tool(
        name="brain_recall",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def brain_recall(query: str, scopes: list[str] | None = None,
                     as_of: str | None = None,
                     knowledge_cutoff: str | None = None,
                     limit: int = 8, recent: bool = False,
                     since: str | None = None,
                     about_assistant: bool = False,
                     around: str | None = None) -> dict[str, Any]:
        """Recall relevant local memories from natural wording without requiring a project ID.

        Set `recent=true` whenever the question is about what happened lately or what the user was
        just doing: "what was I working on?", "where did we leave off?", "what did I do
        yesterday?", "최근에", "아까", "어제", "지금 뭐 하고 있었지?". Results then come newest
        first instead of by word overlap; naming something ("what did Kongi do lately") keeps them
        about that. Set `since` (ISO date or time) to drop anything earlier, e.g. "this week".
        Set `about_assistant=true` when the user asks what YOU said earlier: "which restaurant did
        you recommend?", "what were the steps you gave me?", "네가 아까 뭐라고 했지?". Your own
        past answers are then searched by meaning too, not only by their words.
        Set `around` when the question is about WHEN WE TALKED: "what did we discuss last week?",
        "지난달에 얘기한 거" -> resolve it yourself to a date or "start..end" (e.g. "2026-09-23"
        or "2026-09-01..2026-09-30"). Conversations from then rank first; nothing is dropped.
        Do not set it for when something HAPPENED ("the event I went to two weeks ago"): people
        mention events days or weeks before or after, so that date misleads (measured: on dev2
        it pushed the answering turn out in 2 of 11 questions and helped 2).
        """
        return gather(database, query, scopes=scopes, as_of=as_of or _now(),
                      knowledge_cutoff=knowledge_cutoff, limit=limit, recent=recent, since=since,
                      about_assistant=about_assistant, around=around)

    @server.tool(
        name="brain_context",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def brain_context(query: str, scopes: list[str] | None = None,
                      as_of: str | None = None,
                      knowledge_cutoff: str | None = None,
                      policy: Literal["auto", "always", "never"] = DEFAULT_CONTEXT_POLICY,
                      max_chars: int = 15000,
                      limit: int = 30, recent: bool = False,
                      since: str | None = None,
                      about_assistant: bool = False,
                      around: str | None = None) -> dict[str, Any]:
        """Return only the bounded personal-memory context needed for this request.

        Set `recent=true` whenever the question is about what happened lately or what the user was
        just doing: "what was I working on?", "where did we leave off?", "what did I do
        yesterday?", "최근에", "아까", "어제", "지금 뭐 하고 있었지?". Results then come newest
        first instead of by word overlap; naming something ("what did Kongi do lately") keeps them
        about that. Set `since` (ISO date or time) to drop anything earlier, e.g. "this week".
        Set `about_assistant=true` when the user asks what YOU said earlier: "which restaurant did
        you recommend?", "what were the steps you gave me?", "네가 아까 뭐라고 했지?". Your own
        past answers are then searched by meaning too, not only by their words.
        Set `around` when the question is about WHEN WE TALKED: "what did we discuss last week?",
        "지난달에 얘기한 거" -> resolve it yourself to a date or "start..end" (e.g. "2026-09-23"
        or "2026-09-01..2026-09-30"). Conversations from then rank first; nothing is dropped.
        Do not set it for when something HAPPENED ("the event I went to two weeks ago"): people
        mention events days or weeks before or after, so that date misleads (measured: on dev2
        it pushed the answering turn out in 2 of 11 questions and helped 2).
        """
        route = decide_recall(query, policy)
        if recent and route["mode"] == "SKIP":         # the model said it is about recent events
            route = {**route, "mode": "SEARCH", "reason": "RECENT_REQUESTED"}
        if route["mode"] == "SKIP":
            return {"status": "SKIPPED", "route": route,
                    "scope_completeness": "NOT_APPLICABLE", "char_budget": max_chars,
                    "used_chars": 0, "estimated_tokens": 0, "selected_items": 0,
                    "omitted_items": 0, "items": [], "context": ""}
        recalled = gather(database, query, scopes=scopes, as_of=as_of or _now(),
                          knowledge_cutoff=knowledge_cutoff, limit=limit, recent=recent,
                          since=since, about_assistant=about_assistant, around=around)
        packet = build_context_pack(recalled, max_chars=max_chars,
                                    order="time" if recent else "relevance")
        packet["route"] = route
        if route["mode"] == "EXECUTE":
            packet.setdefault("warnings", []).append(
                "STRUCTURED_EXECUTION_REQUIRED_FOR_COMPLETE_SCOPE_CLAIMS")
        return packet

    @server.tool(
        name="brain_associate",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def brain_associate(query: str, scopes: list[str] | None = None,
                        concept_cues: list[str] | None = None,
                        seed_limit: int = 12, hops: int = 2,
                        fanout: int = 12, max_nodes: int = 40,
                        limit: int = 8,
                        minimum_concept_coverage: float = 0.75,
                        grounding: str = "semantic",
                        semantic_threshold: float = 0.35) -> dict[str, Any]:
        """Recall indirect candidate memories through bounded associative activation.

        grounding="semantic" (default, v0.7.3): on-device embedding pipeline — FTS seed ->
          local-embedding semantic gate -> query re-ranking. On untouched production evals it
          recovers Korean paraphrase recall (Vague Hit@5 0%->57%) at 0% control false association,
          and matches or beats literal on exact. Loads a local model lazily; all text stays on-device.
        grounding="auto": cheap literal gate first, semantic fallback only when literal abstains.
        grounding="literal": the deterministic token-overlap coverage gate only.
        """
        with AssociativeMemoryIndex(database) as index:
            if grounding in ("semantic", "auto"):
                from semantic_recall import (auto_associate, default_embedder,
                                             semantic_associate)
                embedder = default_embedder(database)
                if grounding == "auto":
                    result = auto_associate(index, query, concept_cues, embedder=embedder,
                        literal_gate=minimum_concept_coverage,
                        semantic_threshold=semantic_threshold, scopes=scopes)
                else:
                    result = semantic_associate(index, query, concept_cues,
                        embedder=embedder, scopes=scopes, threshold=semantic_threshold)
            else:
                result = index.activate(query, scopes=scopes, concept_cues=concept_cues,
                    seed_limit=seed_limit, hops=hops, fanout=fanout,
                    max_nodes=max_nodes, limit=limit,
                    minimum_concept_coverage=minimum_concept_coverage)
        result["evidence_status"] = "CANDIDATES_REQUIRE_SOURCE_VERIFICATION"
        return result

    @server.tool(
        name="brain_index",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def brain_index(limit: int = 1000) -> dict[str, Any]:
        """Bring the search index up to date: link new memories, then embed the unembedded ones.

        brain_remember already does this for what it writes; run this after importing a backlog, or
        on a store that was written by an older version.
        """
        from embedding_warmup import warm
        with AssociativeMemoryIndex(database) as index:
            synced = index.sync_sources(limit=limit)
        return {"synced": synced, "vectors_warmed": warm(database, limit=limit),
                "store": str(database)}

    @server.tool(
        name="brain_folder",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def brain_folder(path: str | None = None, share_existing: bool = False) -> dict[str, Any]:
        """Show or choose the folder that holds the user's brain.

        Call it with `path` when the user names a folder for their memory, e.g. "use my Google
        Drive's Graph-MIND folder as my brain". A folder that OneDrive, Google Drive or Dropbox
        syncs makes the same memory available on every PC where the user chooses it too; nothing
        else is needed. Without `path` it reports the current folder and the devices writing to it.
        `path="local"` keeps the brain in this PC's own Postgres instead, shared the instant it is
        written (no sync delay).

        Other PCs: when the user asks to let their other PCs use this brain ("다른 PC도 붙게 해줘"),
        call it with `path="share"`. It opens this PC's Postgres to the local network and Tailscale
        (Windows may ask the user to approve a firewall rule once) and returns a
        `connection_code` starting "gm1.". Show the user that code and tell them to paste it to
        the AI on the other PC; it contains the password, so say not to post it anywhere public.
        On the other PC, when the user pastes a "gm1." code, call this with `path=<the code>`.

        `share_existing=true` copies what this PC already remembered into the folder, which uploads
        it to wherever the folder syncs. Set it only when the user has explicitly agreed to that.
        """
        from brain_log import device_name, devices, export_history, folder, set_folder, share, sync
        shared_out = None
        if path == "share":
            shared_out = share()
        elif path:
            set_folder(path)
        shared = folder()
        if not shared:
            return {"folder": None, "this_device": device_name(),
                    "note": "No brain folder yet: memory stays on this PC. Ask the user which "
                            "folder to use (a synced one shares it across PCs)."}
        imported = sync(shared, database)            # bring in what other PCs already wrote
        exported = export_history(shared, database) if share_existing else None
        from brain_log import is_database
        if not is_database(shared):
            where = str(shared)
        elif "@127.0.0.1:" in shared:
            where = "this PC's Postgres"
        else:                                   # never show the password inside the URL
            where = "Postgres on " + shared.rsplit("@", 1)[1].split("/")[0]
        result = {} if shared_out is None else {
            "connection_code": shared_out["connection_code"],
            "reachable_at": shared_out["addresses"], "firewall": shared_out["firewall"]}
        return {**result, "folder": where, "this_device": device_name(),
                "devices": devices(shared),
                "imported_from_other_devices": imported["imported"],
                "existing_memories_shared": exported or "not shared; ask the user before "
                                                       "setting share_existing=true"}

    @server.tool(
        name="brain_timeline",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def brain_timeline(entity: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Every memory about ONE thing, oldest first, with replaced entries marked.

        Similarity search answers "what relates to this question"; this answers "what happened to
        the auth module / this project / this device, in order". Those are the questions that keep
        failing otherwise, because the four decisions about one thing sit in four different
        sessions and only the nearest one or two come back. Call it with no entity to list what the
        store knows about, busiest first.
        """
        import sqlite3

        import entity_timeline as timeline_index

        shared_sync(database)                  # every device's memories are in this one index
        db = sqlite3.connect(database)
        try:
            if not entity:
                return {"entities": timeline_index.entities(db, limit=limit)}
            rows = timeline_index.timeline(db, entity, limit=limit)
        finally:
            db.close()
        current = [r for r in rows if not r["superseded"]]
        return {"entity": entity, "entries": rows[:limit], "count": len(rows),
                "current": current[-1] if current else None}

    @server.tool(
        name="conversation_recall",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def conversation_recall(query: str, scopes: list[str] | None = None,
                            limit: int = 10) -> dict[str, Any]:
        """Search redacted conversation turns captured after Local Brain activation."""
        try:
            with ConversationMemoryStore(database) as store:
                return store.search(query, scopes=scopes, limit=limit)
        except ValueError as error:
            return {"status": "UNKNOWN", "reason": "INVALID_CONVERSATION_QUERY",
                    "detail": str(error), "scope_completeness": "UNATTESTED",
                    "turns": []}

    @server.tool(
        name="code_activity",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def code_activity(project_id: str | None = None,
                      happened_from: str | None = None,
                      happened_to: str | None = None,
                      file_path: str | None = None,
                      symbol: str | None = None,
                      change_kind: str | None = None,
                      limit: int = 20,
                      max_diff_chars: int = 6000) -> dict[str, Any]:
        """Return indexed code changes and diff evidence without rereading a repository."""
        try:
            bounded_diff_chars = max(200, min(max_diff_chars, 20000))
            with CodingMemoryStore(database) as store:
                return store.query(project_id=project_id, happened_from=happened_from,
                    happened_to=happened_to, file_path=file_path, symbol=symbol,
                    change_kind=change_kind, limit=limit,
                    include_diff_excerpt=True, max_diff_chars=bounded_diff_chars)
        except ValueError as error:
            return {"status": "UNKNOWN", "reason": "INVALID_CODE_ACTIVITY_QUERY",
                    "detail": str(error), "scope_completeness": "UNATTESTED",
                    "changes": []}

    @server.tool(
        name="memory_record",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def memory_record(project_id: str, session_id: str, event_type: RecordableEventType,
                      subject_id: str, payload: dict[str, Any], source_ref: str,
                      effective_at: str | None = None,
                      source_type: str = "mcp-client",
                      supersedes_event_id: str | None = None,
                      event_id: str | None = None) -> dict[str, Any]:
        """Append one sourced development event without overwriting prior history."""
        known_at = _now()
        event = {"event_id": event_id or "mcp-" + uuid.uuid4().hex,
            "project_id": project_id, "session_id": session_id,
            "event_type": event_type, "effective_at": effective_at or known_at,
            "known_at": known_at, "actor": "assistant", "subject_id": subject_id,
            "payload": payload,
            "provenance": {"source_type": source_type, "source_ref": source_ref},
            "supersedes_event_id": supersedes_event_id}
        with DevelopmentMemoryStore(database) as store:
            result = store.record_event(event)
        return {**result, "project_id": project_id, "store": str(database)}

    @server.tool(
        name="project_status",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def project_status(project_id: str, as_of: str | None = None,
                       knowledge_cutoff: str | None = None) -> dict[str, Any]:
        """Return the current objective, active work and next actions for a project."""
        when = as_of or _now()
        with DevelopmentMemoryStore(database) as store:
            resumed = store.resume_project(project_id, as_of=when,
                                           knowledge_cutoff=knowledge_cutoff)
        if resumed["status"] != "KNOWN":
            return resumed
        return {key: resumed[key] for key in (
            "status", "reason", "project_id", "as_of", "knowledge_cutoff",
            "scope_completeness", "warnings", "objective", "current_task",
            "open_tasks", "next_actions", "active_experiments", "last_session",
            "evidence_event_ids")}

    @server.tool(
        name="resume_project",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def resume_project(project_id: str, as_of: str | None = None,
                       knowledge_cutoff: str | None = None,
                       recent_file_limit: int = 10,
                       recent_test_limit: int = 10) -> dict[str, Any]:
        """Build a full evidence-backed packet for continuing work in a new session."""
        when = as_of or _now()
        with DevelopmentMemoryStore(database) as store:
            return store.resume_project(project_id, as_of=when,
                knowledge_cutoff=knowledge_cutoff, recent_file_limit=recent_file_limit,
                recent_test_limit=recent_test_limit)

    @server.tool(
        name="explain_decision",
        structured_output=True,
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def explain_decision(project_id: str, decision_id: str,
                         as_of: str | None = None,
                         knowledge_cutoff: str | None = None) -> dict[str, Any]:
        """Return the active decision, reason, correction history and provenance."""
        when = as_of or _now()
        with DevelopmentMemoryStore(database) as store:
            return store.explain_decision(project_id, decision_id, as_of=when,
                                          knowledge_cutoff=knowledge_cutoff)

    return server


server = build_server()


def preload(database):
    """Load the embedding model while the app is still starting. Importing torch and the model
    took 30-50 s on a OneDrive-synced Windows PC, and the user's first memory question paid it."""
    def work():
        try:
            embedder_for(database).embed(["preload"])
        except Exception:                 # no model on disk: word search still answers
            pass
    # after a pause: importing torch while the app is still starting its servers delayed the
    # handshake (19 s inside Codex), and Codex leaves out a server that is not ready yet
    timer = threading.Timer(15, work)
    timer.daemon = True
    timer.start()


if __name__ == "__main__":
    preload(configured_db_path())
    server.run(transport="stdio")
