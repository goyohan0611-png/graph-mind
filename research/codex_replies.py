"""Real model replies for the learning-curve signal (learning_curve.py, signal "reply").

In use, Turnbook sees the packet it returned and, a moment later, the model's reply (it is
captured like every turn). Which shown turns the reply drew on is the learning signal. Here Codex
answers each LoCoMo question from the shipped packet (the first 30 fused turns), one question at
a time, so the signal can be measured on real replies instead of the benchmark's short answers.

    python research/codex_replies.py conv-26 conv-30 conv-41     # resumable
"""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import time

RUN = Path(__file__).resolve().parent / "runs/graph-v9.20-learning"
POOLS = RUN / "pools.json"
OUT = RUN / "replies.jsonl"
EMPTY = RUN / ".codex-empty"


def codex(prompt: str, tries: int = 6) -> str:
    for attempt in range(tries):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            try:
                subprocess.run([shutil.which("codex"), "exec", "-m", "gpt-6.1-sol",
                                "-c", "model_reasoning_effort=low", "-s", "read-only",
                                "--skip-git-repo-check", "--ephemeral", "-C", str(EMPTY),
                                "-o", str(out), "-"],
                               input=prompt, text=True, encoding="utf-8", capture_output=True,
                               timeout=600)
            except (OSError, subprocess.TimeoutExpired):
                time.sleep(20 * (attempt + 1))
                continue
            if out.exists() and out.read_text(encoding="utf-8").strip():
                return out.read_text(encoding="utf-8").strip()
        time.sleep(20 * (attempt + 1))
    return ""


def prompt(question: dict, texts: dict) -> str:
    lines = [f"[{texts[t]['at'][:10]}] {texts[t]['text']}" for t in question["shipped"]]
    return ("You are an assistant with a memory of past conversations. Excerpts from that memory "
            "are below. Answer the user's question from them in a few sentences, as you would "
            "in a chat. Do not run any commands or read files.\n\nMEMORY:\n" + "\n".join(lines)
            + f"\n\nQUESTION: {question['question']}")


def main() -> None:
    EMPTY.mkdir(exist_ok=True)
    data = json.loads(POOLS.read_text(encoding="utf-8"))
    done = set()
    if OUT.exists():
        done = {(r["conv"], r["n"]) for r in map(json.loads, OUT.open(encoding="utf-8"))}
    wanted = [q for q in data["questions"] if q["conv"] in sys.argv[1:] and q["evidence"]
              and (q["conv"], q["n"]) not in done]
    print(len(wanted), "to answer", flush=True)
    for i, question in enumerate(wanted, 1):
        reply = codex(prompt(question, data["turns"][question["conv"]]))
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"conv": question["conv"], "n": question["n"], "reply": reply},
                               ensure_ascii=False) + "\n")
        print(i, question["conv"], question["n"], len(reply), flush=True)


if __name__ == "__main__":
    main()
