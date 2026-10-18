"""Hooks for agents that run a command per message but keep no transcript Graph-MIND can read:
what was said is appended, in Claude Code's record shape, to ~/.graph-mind/hooks/<agent>/, and
the capture service stores it like every other conversation. Where the hook can add context
(Gemini CLI's BeforeAgent), the memory that bears on the message goes in before the model
answers, as with the OpenCode plugin.

    python -m graph_mind.hook gemini-cli BeforeAgent      # stdin: the agent's hook JSON

graph-mind-install registers these (install.agent_hooks). Any failure is silent: memory must
never stand in the way of a message.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import uuid

# agent -> event -> (who said it, the field that holds the text)
EVENTS = {
    "gemini-cli": {"BeforeAgent": ("user", "prompt"), "AfterAgent": ("assistant", "prompt_response")},
    "cursor": {"beforeSubmitPrompt": ("user", "prompt"), "afterAgentResponse": ("assistant", "text")},
    # ponytail: Copilot CLI's hooks never carry the reply (agentStop gives only a transcript
    # path, format undocumented); the user's side alone is stored until that is read too
    "copilot-cli": {"userPromptSubmitted": ("user", "prompt")},
}


def hooks_root():
    from graph_mind.paths import graph_mind_home
    return graph_mind_home() / "hooks"


def record(agent: str, event_name: str, event: dict) -> str | None:
    """Append what the event says to the agent's transcript; the text, or None."""
    role, field = EVENTS[agent][event_name]
    text = event.get(field)
    if not isinstance(text, str) or not text.strip():
        return None
    session = str(event.get("session_id") or event.get("conversation_id")
                  or event.get("sessionId") or "default")
    roots = event.get("workspace_roots") or []
    folder = hooks_root() / agent
    folder.mkdir(parents=True, exist_ok=True)
    line = {"type": role, "uuid": str(uuid.uuid4()), "sessionId": session,
            "cwd": event.get("cwd") or (roots[0] if roots else None),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "message": {"content": text}}
    with (folder / f"{''.join(c for c in session if c.isalnum() or c in '-_')}.jsonl").open(
            "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    return text


def reply(agent: str, event_name: str, text: str | None) -> dict:
    """What the agent expects back on stdout."""
    if agent == "gemini-cli" and event_name == "BeforeAgent" and text:
        from graph_mind.inject import context
        memory = context(text[:4000])
        if memory:
            return {"hookSpecificOutput": {"hookEventName": "BeforeAgent",
                                           "additionalContext": memory}}
    if agent == "cursor" and event_name == "beforeSubmitPrompt":
        return {"continue": True}
    return {}


def main(argv=None) -> None:
    agent, event_name = (argv if argv is not None else sys.argv[1:])[:2]
    text = None
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        text = record(agent, event_name, json.loads(sys.stdin.read() or "{}"))
        answer = reply(agent, event_name, text)
    except Exception:
        answer = reply(agent, event_name, None)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    main()
