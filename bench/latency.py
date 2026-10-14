"""How fast does an AI app get its memory? The MCP server as the apps start it, timed.

A store of synthetic conversation turns is built once and embedded the way the capture service
does it, so the measurement starts where a real user's PC stands: everything already captured.
Then the server is started over stdio, exactly as Claude Code or Codex start it, and timed:

    ready          start until the server answers `initialize`
    first answer   the first brain_context call (word search is always there)
    meaning ready  start until a question that shares no word with its answer finds it, i.e.
                   until search by meaning works (the embedding model loaded in the server)
    across         whether the same question asked in Korean finds the English memory
    steady         brain_context p50 / p95 over 30 questions once everything is loaded
    memory         the server process's resident memory; every app runs its own server

    python bench/latency.py                  # 5,000 turns
    python bench/latency.py --turns 20000
    python bench/latency.py --baseline       # also write bench/baseline/latency.json

The first run per size embeds the store (minutes on a CPU); it is kept in bench/.cache.
"""
from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timedelta
from pathlib import Path
import asyncio
import json
import os
import random
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph_mind.associative_memory import AssociativeMemoryIndex  # noqa: E402
from graph_mind.conversation_memory import ConversationMemoryStore  # noqa: E402
from graph_mind.warmup import warm_turns  # noqa: E402

CACHE = ROOT / "bench/.cache/latency"
BASELINE = ROOT / "bench/baseline/latency.json"
TARGET = "We finally adopted a golden retriever puppy last spring and named him Kongi."
PROBE = "what kind of dog do I have"     # shares no word with TARGET: only meaning finds it
PROBE_KO = "반려견 품종이 뭐였지?"       # the same question in Korean, about an English memory
TOPICS = {
    "work": ["deadline", "standup", "migration", "release", "review", "budget", "roadmap"],
    "code": ["refactor", "index", "query", "cache", "timeout", "deploy", "schema"],
    "food": ["ramen", "bibimbap", "pasta", "coffee", "bakery", "kimchi", "salad"],
    "travel": ["Jeju", "Osaka", "train", "hotel", "flight", "museum", "beach"],
    "health": ["running", "yoga", "sleep", "dentist", "vitamins", "swimming", "stretching"],
    "money": ["rent", "savings", "invoice", "insurance", "tax", "groceries", "subscription"],
}
VERBS = ["talked about", "planned", "finished", "worried about", "compared", "postponed",
         "looked into", "decided on", "asked about", "fixed"]
QUESTIONS = ["what did I decide about the release", "where did I travel last time",
             "how is my sleep going", "what did I eat in Osaka", "invoice from last month",
             "the cache timeout problem", "my running routine", "rent and savings plan",
             "schema migration status", "coffee place I liked"]


def synthetic_turns(count: int):
    """Deterministic, varied, and about anything but pets."""
    rng = random.Random(7)
    start = datetime(2026, 1, 1, 9)
    for n in range(count):
        topic = rng.choice(list(TOPICS))
        words = rng.sample(TOPICS[topic], 3)
        text = (f"Today I {rng.choice(VERBS)} the {words[0]} and the {words[1]}; "
                f"tomorrow the {words[2]} (note {n}, {topic}).")
        yield n, start + timedelta(minutes=17 * n), text


def build_store(turns: int) -> Path:
    """The store as a user's PC holds it after capture: turns recorded, indexed, embedded."""
    folder = CACHE / f"{turns}"
    store = folder / "memory.sqlite"
    if (folder / "ready").exists():
        return store
    folder.mkdir(parents=True, exist_ok=True)
    store.unlink(missing_ok=True)
    print(f"building a store of {turns:,} turns (once)", flush=True)
    with ConversationMemoryStore(store) as memory:
        rows = list(synthetic_turns(turns))
        rows.insert(turns // 3, (turns, rows[turns // 3][1], TARGET))
        for n, when, text in rows:
            memory.record({"turn_id": f"t{n}", "client": "bench",
                           "client_session_id": f"s{n // 20}", "scope": "global",
                           "role": "user", "content_redacted": text,
                           "raw_sha256": f"t{n}", "source_path": "bench", "source_ordinal": n,
                           "happened_at": when.isoformat(), "known_at": when.isoformat()})
    with AssociativeMemoryIndex(store) as index:
        while index.sync_sources(limit=5000)["status"] == "ENGRAMS_INDEXED":
            pass
    while warm_turns(store, limit=2048):
        print(".", end="", flush=True)
    (folder / "ready").write_text("ok", encoding="utf-8")
    print()
    return store


async def run_server(store: Path, target: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import psutil
    home = tempfile.mkdtemp(prefix="graph-mind-bench-home-")
    env = {**os.environ, "GRAPH_MIND_DB": str(store), "GRAPH_MIND_HOME": home,
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "graph_mind.mcp_server"],
                                   env=env, cwd=str(ROOT / "src"))

    async def ask(session, query):
        began = time.perf_counter()
        result = await session.call_tool("brain_context", {"query": query})
        return (time.perf_counter() - began) * 1000, result.structured_content or {}

    began = time.perf_counter()
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        ready = time.perf_counter() - began
        first_ms, _ = await ask(session, QUESTIONS[0])
        meaning = None
        while time.perf_counter() - began < 300:
            _, packet = await ask(session, PROBE)
            if target in packet.get("ids", []):
                meaning = time.perf_counter() - began
                break
            await asyncio.sleep(0.25)
        _, packet = await ask(session, PROBE_KO)
        across = target in packet.get("ids", [])
        steady = [(await ask(session, QUESTIONS[i % len(QUESTIONS)]))[0] for i in range(30)]
        servers = [p for p in psutil.Process().children(recursive=True)
                   if "graph_mind.mcp_server" in " ".join(p.cmdline())]
        rss = max((p.memory_info().rss for p in servers), default=0)
    steady.sort()
    return {"ready_s": round(ready, 2), "first_answer_ms": round(first_ms),
            "meaning_ready_s": round(meaning, 1) if meaning else None,
            "steady_p50_ms": round(statistics.median(steady)),
            "steady_p95_ms": round(steady[int(len(steady) * 0.95) - 1]),
            "server_memory_mb": round(rss / 2 ** 20), "korean_finds_english": across}


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--turns", type=int, default=5000)
    parser.add_argument("--baseline", action="store_true", help="write the result as baseline")
    args = parser.parse_args()
    store = build_store(args.turns)
    measured = asyncio.run(run_server(store, f"t{args.turns}"))  # TARGET's turn id
    result = {"measured_at": datetime.now().isoformat(timespec="seconds"),
              "turns": args.turns, "platform": sys.platform, **measured}
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    for key, value in measured.items():
        before = baseline.get(key)
        print(f"{key:<18}{value!s:>10}" + (f"   baseline {before}" if before is not None else ""))
    if args.baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(f"baseline written: {BASELINE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
