"""Record the README demo from a real run, then draw it as a terminal GIF.

Nothing in the GIF is typed by hand: a fresh store in a temporary home, a Claude Code transcript
the capture service reads, and a brain_context call over MCP as Codex would make it. The text
shown is what those steps returned.

    python demo/record_demo.py        # writes demo/demo.gif
"""
from __future__ import annotations

from pathlib import Path
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
QUESTION = "what did we decide about the auth rollout?"
SAID = ("Let's ship the auth refactor behind the NEW_AUTH flag and roll it out Thursday. "
        "Staging key is sk-proj-" + "x" * 40)


def record() -> dict:
    """Run the real thing in a throwaway home and return what each step printed."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    home = Path(tempfile.mkdtemp(prefix="gm-demo-"))
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home),
           "GRAPH_MIND_HOME": str(home / ".graph-mind"), "PYTHONUTF8": "1",
           # the model already on this PC; a fresh download is not what a user waits for daily
           "HF_HOME": os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))}
    for key in ("GRAPH_MIND_FOLDER", "GRAPH_MIND_CONFIG", "GRAPH_MIND_DB"):
        env.pop(key, None)
    transcript = home / ".claude" / "projects" / "app" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "summary", "sessionId": "s1"}) + "\n",
                          encoding="utf-8")
    config = home / ".graph-mind" / "automatic-capture.json"
    run = lambda *args: subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding="utf-8")
    run("-c", "from automatic_capture import _atomic_json, make_config; import sys; "
              f"c = make_config(workspace=r'{ROOT}', project_id='project:app', "
              f"session_id='demo', sessions_root=r'{home / '.codex' / 'sessions'}'); "
              f"c['claude_cowork_root'] = r'{home / 'none'}'; _atomic_json(r'{config}', c)")
    run("automatic_capture_cli.py", "--config", str(config), "once")          # baseline
    with transcript.open("a", encoding="utf-8") as stream:
        for role, text in (("user", SAID), ("assistant", "Done: flag added, rollout set.")):
            stream.write(json.dumps({"type": role, "sessionId": "s1", "uuid": role,
                                     "cwd": str(ROOT), "timestamp": "2026-10-05T10:14:00",
                                     "message": {"role": role, "content": text}}) + "\n")
    captured = json.loads(run("automatic_capture_cli.py", "--config", str(config), "once").stdout)
    turns = captured["conversation_sources"]["claude-code"]["recorded_turns"]
    run("-c", "from automatic_capture import AutomaticCaptureService, load_config; "
              f"AutomaticCaptureService(load_config(r'{config}')).embed_new_turns()")

    async def ask():
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "graph_mind_mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                # steady state: the app has been open a while and has answered before (the
                # first question after launch also waits for the model to load, 30-50 s here)
                await client.call_tool("brain_context", {"query": "warm-up", "policy": "always"})
                started = time.perf_counter()
                result = await client.call_tool("brain_context",
                                                {"query": QUESTION, "policy": "always"})
                return result.structured_content, time.perf_counter() - started
    packet, seconds = asyncio.run(ask())
    top = packet["items"][0]
    return {"turns": turns, "seconds": seconds, "tokens": packet["estimated_tokens"],
            "source": top["provenance"].get("source_type", ""), "title": top["title"],
            "when": top["effective_at"],
            "content": top["content"]}


def draw(rec: dict, out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("consola.ttf", 17)
    bold = ImageFont.truetype("consolab.ttf", 17)
    width, line, pad = 900, 25, 22
    colors = {"dim": (130, 140, 150), "text": (225, 228, 232), "green": (126, 211, 121),
              "blue": (110, 170, 255), "yellow": (240, 200, 110), "pink": (240, 130, 170)}
    content = rec["content"]
    import textwrap
    wrapped = textwrap.wrap(content, 76)
    script = [
        ("dim", "# Monday, in Claude Code"),
        ("prompt", "> " + SAID[:SAID.index("Staging")].strip()),
        ("prompt", "  Staging key is sk-proj-xxxxxxxx..."),
        ("green", f"  + Graph-MIND captured {rec['turns']} turns  (secret masked, no LLM call)"),
        ("blank", ""),
        ("dim", "# Wednesday, in Codex: a different model, a new session"),
        ("prompt", "> " + QUESTION),
        ("blue", f"  brain_context → {rec['tokens']} tokens in {rec['seconds']:.1f}s"),
        *[("yellow", ("  “" if n == 0 else "   ") + part
           + ("”" if n == len(wrapped) - 1 else "")) for n, part in enumerate(wrapped)],
        ("dim", f"   — said in {rec['source']}, {rec['when'][:16].replace('T', ' ')}"),
    ]
    height = pad * 2 + line * (len(script) + 1)
    frames, durations, shown = [], [], []

    def frame(partial=None, hold=90):
        image = Image.new("RGB", (width, height), (24, 26, 31))
        canvas = ImageDraw.Draw(image)
        canvas.ellipse((14, 10, 24, 20), fill=(255, 95, 86))
        canvas.ellipse((32, 10, 42, 20), fill=(255, 189, 46))
        canvas.ellipse((50, 10, 60, 20), fill=(39, 201, 63))
        rows = shown + ([partial] if partial else [])
        for n, (kind, text) in enumerate(rows):
            y = pad + 12 + n * line
            if kind == "prompt":
                canvas.text((pad, y), text[:2], font=bold, fill=colors["pink"])
                canvas.text((pad + 19, y), text[2:], font=font, fill=colors["text"])
            elif kind != "blank":
                canvas.text((pad, y), text, font=font, fill=colors[kind])
        frames.append(image)
        durations.append(hold)

    for kind, text in script:
        if kind == "prompt" and text.startswith("> "):
            for cut in range(4, len(text) + 1, 4):          # typed, a few characters a frame
                frame((kind, text[:cut]), 40)
        shown.append((kind, text))
        frame(hold=700 if kind in ("green", "blue", "blank") else 120)
    frame(hold=4000)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=True)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    recorded = record()
    print(json.dumps({k: v for k, v in recorded.items() if k != "content"}), recorded["content"])
    draw(recorded, Path(__file__).with_name("demo.gif"))
    print("wrote demo/demo.gif")
