"""The memory for a message, before the model sees it: for agents that run a hook per message
(OpenCode's plugin), so the past reaches every model, not only the ones that think to call
brain_context. Weak models saved a fact and then never looked it up, or looked it up badly.

    echo "what breed is my dog?" | python -m graph_mind.inject      # prints the packet, or nothing

Small on purpose (8 items, 4,000 characters): it rides along with every message the user sends.
Nothing for messages with no sign of the past (decide_recall), or when nothing is found.
"""
from __future__ import annotations

import sys

LIMIT, CHARS = 8, 4000
HEADER = ("Graph-MIND memory: what the user said or saved before that may bear on this message "
          "(dated; older lines can be out of date). Use it if it helps, ignore it if not; for "
          "more, call brain_context.\n\n")


def context(query: str) -> str:
    from graph_mind.context_packet import build_context_pack, decide_recall
    from graph_mind.local_embedder import LocalEmbedder
    from graph_mind.mcp_server import _now, configured_db_path, embedder_for, gather
    if not query.strip() or decide_recall(query, "auto")["mode"] == "SKIP":
        return ""
    LocalEmbedder.USE_SERVICE = True          # the capture service's loaded model, if running
    LocalEmbedder.WAIT_FOR_MODEL = 2.0
    database = configured_db_path()
    recalled = gather(database, query, as_of=_now(), limit=LIMIT,
                      embedder=embedder_for(database))
    packet = build_context_pack(recalled, max_chars=CHARS)
    return HEADER + packet["context"] if packet["status"] == "KNOWN" else ""


def main() -> None:
    try:
        sys.stdout.write(context(sys.stdin.read()[:4000]))
    except Exception:                         # never in the way of the message itself
        pass


if __name__ == "__main__":
    main()
