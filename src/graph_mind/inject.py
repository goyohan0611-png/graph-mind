"""The memory for a message, before the model sees it: for agents that run a hook per message
(OpenCode's plugin), so the past reaches every model, not only the ones that think to call
brain_context. Weak models saved a fact and then never looked it up, or looked it up badly.

    echo "what breed is my dog?" | python -m graph_mind.inject      # prints the packet, or nothing

Small on purpose (8 items, 4,000 characters): it rides along with every message the user sends.
Nothing for messages with no sign of the past (decide_recall), or when nothing is found.
"""
from __future__ import annotations

import os
import sys

# GRAPH_MIND_INJECT=items,chars to change it (research/opencode_longmemeval.py measures sizes)
LIMIT, CHARS = (int(v) for v in os.environ.get("GRAPH_MIND_INJECT", "8,4000").split(","))
HEADER = ("Turnbook memory: what the user said or saved before that may bear on this message "
          "(dated; older lines can be out of date). Use it if it helps, ignore it if not; for "
          "more, call brain_context.\n\n")


SERVICE_WAIT = 5.0     # seconds for the capture service's answer before searching here


def context(query: str, *, database=None, embedder=None) -> str:
    """`database`/`embedder` given: the capture service answering for a hook, from its own
    loaded model and warm index. Neither: a hook's own process, which asks the service first."""
    from graph_mind.context_packet import build_context_pack, decide_recall
    if not query.strip() or decide_recall(query, "auto")["mode"] == "SKIP":
        return ""                             # before the heavy imports: most messages end here
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
    """The capture service's answer (its index is warm), or None: no service, an older one, one
    on another model, or no answer within SERVICE_WAIT."""
    try:
        from multiprocessing.connection import Client
        import json

        from graph_mind.local_embedder import LOCAL_MODEL, service_file
        info = json.loads(service_file().read_text(encoding="utf-8"))
        if not info.get("context") or info.get("model") != LOCAL_MODEL:
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
