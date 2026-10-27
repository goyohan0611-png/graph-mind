"""LoCoMo in Korean, to measure recall where Korean particles glue onto words ("Caroline은").

Codex translates one session (or 40 questions) per call, one call at a time, into casual Korean
chat. Person names stay in English spelling, so particles attach to them as they would to any
noun. Ids are kept, so the evidence labels still apply. Resumable; the result is not committed
(it is the dataset's text).

    python research/locomo_ko.py                 # all 10 conversations
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_replies import EMPTY, codex  # noqa: E402
from locomo_pools import DATA, fetch  # noqa: E402

OUT = Path(__file__).resolve().parent / "runs/graph-v9.21-korean/locomo_ko.json"
RULES = ("Translate each item's text into natural, casual Korean, the way people chat. Keep every "
         "person's name exactly as written in English letters (e.g. 'Caroline은'). Keep numbers "
         "and dates accurate, and keep the labels 'Q:' and 'A:' where they appear. Do not run any commands or read files. Reply with ONLY a JSON list "
         'of objects {"id": ..., "text": ...}, one per input item, same ids.\n\nINPUT:\n')


def ask(items: list[dict]) -> dict:
    for _ in range(3):
        reply = codex(RULES + json.dumps(items, ensure_ascii=False))
        try:
            rows = json.loads(reply[reply.index("["):reply.rindex("]") + 1])
            got = {str(r["id"]): r["text"] for r in rows if isinstance(r, dict)}
        except (ValueError, KeyError, TypeError):
            continue
        if all(str(i["id"]) in got for i in items):
            return got
    raise RuntimeError("translation failed for " + str(items[0]["id"]))


def main() -> None:
    fetch()
    EMPTY.mkdir(exist_ok=True)
    samples = json.loads(DATA.read_text(encoding="utf-8"))
    done = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    for sample in samples:
        sid = sample["sample_id"]
        mine = done.setdefault(sid, {"turns": {}, "qa": {}})
        talk = sample["conversation"]
        for key in sorted((k for k in talk if k.startswith("session_")
                           and isinstance(talk[k], list)), key=lambda k: int(k.split("_")[1])):
            items = []
            for turn in talk[key]:
                if turn["dia_id"] in mine["turns"]:
                    continue
                text = turn["text"]
                if turn.get("blip_caption"):
                    text += f" [shares a photo: {turn['blip_caption']}]"
                items.append({"id": turn["dia_id"], "text": text})
            if items:
                mine["turns"].update(ask(items))
                OUT.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                print(sid, key, len(mine["turns"]), flush=True)
        wanted = [{"id": n, "text": f"Q: {qa['question']}\nA: {qa.get('answer', '')}"}
                  for n, qa in enumerate(sample["qa"])
                  if qa["category"] != 5 and str(n) not in mine["qa"]]
        for start in range(0, len(wanted), 40):
            mine["qa"].update(ask(wanted[start:start + 40]))
            OUT.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
            print(sid, "qa", len(mine["qa"]), flush=True)


if __name__ == "__main__":
    main()
