"""Answer turns (has_answer) of some questions, read from the LongMemEval file one question at a
time (the whole file as objects would take ~2 GB). Usage: answer_turns.py IDS_FILE OUT.json"""
from pathlib import Path
import json
import sys

DATA = Path(__file__).parent / "external" / "longmemeval" / "longmemeval_s_cleaned.json"


def answer_turns(wanted: set) -> dict:
    text, decoder, at, found = DATA.read_text(encoding="utf-8"), json.JSONDecoder(), 0, {}
    at = text.index("[") + 1
    while len(found) < len(wanted):
        while text[at] in " \r\n\t,":
            at += 1
        if text[at] == "]":
            break
        item, at = decoder.raw_decode(text, at)
        if item["question_id"] in wanted:
            found[item["question_id"]] = [
                {"session_id": sid, "role": turn["role"], "content": turn["content"]}
                for sid, session in zip(item["haystack_session_ids"], item["haystack_sessions"])
                for turn in session if turn.get("has_answer")]
    return found


if __name__ == "__main__":
    ids = {json.loads(line)["id"] for line in Path(sys.argv[1]).read_text(
        encoding="utf-8").split("\n") if line.strip()}
    Path(sys.argv[2]).write_text(json.dumps(answer_turns(ids), ensure_ascii=False),
                                 encoding="utf-8")
