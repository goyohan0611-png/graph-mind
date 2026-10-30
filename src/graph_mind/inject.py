"""The memory for a message, before the model sees it: for agents that run a hook per message
(OpenCode's plugin), so the past reaches every model, not only the ones that think to call
brain_context. Weak models saved a fact and then never looked it up, or looked it up badly.

    echo "what breed is my dog?" | python -m graph_mind.inject      # prints the packet, or nothing

Every message is searched: whether it needs the past is a language judgement left to the model,
not a keyword list (one missed "오늘 점심 뭐 먹었지?"), and similarity could not tell either
("테스트 돌려줘" scored above "where did we leave off?"). So the packet is small (4 items,
1,200 characters, ~300 tokens): it rides along with every message; brain_context gives more.
"""
from __future__ import annotations

import os
import sys

# GRAPH_MIND_INJECT=items,chars to change it (research/opencode_longmemeval.py measures sizes)
LIMIT, CHARS = (int(v) for v in os.environ.get("GRAPH_MIND_INJECT", "4,1200").split(","))
HEADER = ("Turnbook memory: what the user said or saved before that may bear on this message "
          "(dated; older lines can be out of date). Use it if it helps, ignore it if not; for "
          "more, call brain_context.\n\n")


ENDS = 500          # characters searched from each end of a long message
SERVICE_WAIT = 2.5     # seconds for the capture service's answer before searching here


def context(query: str, *, database=None, embedder=None) -> str:
    """`database`/`embedder` given: the capture service answering for a hook, from its own
    loaded model and warm index. Neither: a hook's own process, which asks the service first."""
    from graph_mind.context_packet import build_context_pack
    if len(query) > 2 * ENDS:
        # a pasted log or file: the question sits at its start or end, and the rest only slowed
        # the search (4,000 characters took 2.2 s in the service, 1,000 0.8 s)
        query = query[:ENDS] + "\n" + query[-ENDS:]
    if not query.strip():
        return ""
    if database is None:
        served = from_service(query)
        if served is not None:
            return served
    from graph_mind.mcp_server import _now, configured_db_path, embedder_for, gather
    if embedder is None:
        from graph_mind.local_embedder import LocalEmbedder
        LocalEmbedder.USE_SERVICE = True      # the capture service's loaded model, if running
        LocalEmbedder.WAIT_FOR_MODEL = 2.0
    database = database or configured_db_path()
    recalled = gather(database, query, as_of=_now(), limit=LIMIT,
                      embedder=embedder or embedder_for(database), share=False)
    packet = build_context_pack(recalled, max_chars=CHARS)
    return HEADER + packet["context"] if packet["status"] == "KNOWN" else ""


def from_service(query: str):
    """The capture service's answer (its index is warm), or None: no service, an older one, or
    no answer within SERVICE_WAIT. It searches with its own model, the one model.txt names (not
    checked here: importing local_embedder for it took 0.4 s of every message)."""
    try:
        from multiprocessing.connection import Client
        import json

        from graph_mind.paths import graph_mind_home
        info = json.loads((graph_mind_home() / "embedder.json").read_text(encoding="utf-8"))
        if not info.get("context"):
            return None
        import socket
        try:                # a service that died left its file: connecting took 2 s to fail
            socket.create_connection(("127.0.0.1", info["port"]), timeout=0.3).close()
        except OSError:
            return None
        with Client(("127.0.0.1", info["port"]), authkey=bytes.fromhex(info["key"])) as conn:
            conn.send(("context", query))
            if not conn.poll(SERVICE_WAIT):
                return None
            answer = conn.recv()
        return answer if isinstance(answer, str) else None
    except Exception:
        return None


def main() -> None:
    try:
        sys.stdout.write(context(sys.stdin.read()[:4000]))
    except Exception:                         # never in the way of the message itself
        pass


if __name__ == "__main__":
    main()
