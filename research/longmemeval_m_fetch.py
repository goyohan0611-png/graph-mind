"""Fetch LongMemEval_M and split it per question, without ever holding 2.74 GB in memory.

_M asks the same 500 questions as _S over ~500 haystack sessions each instead of ~40, which is the
only dataset on hand that can test whether retrieval still finds the right session when the memory is
an order of magnitude larger. Two reasons this is not a plain `json.load`:

  * the file is 2.74 GB of JSON; parsing it whole needs well over 10 GB of RAM, and this host has
    already killed background runs under memory pressure. Instances are streamed one at a time.
  * it is downloaded OUTSIDE the OneDrive-synced project folder on purpose. A 2.74 GB dataset inside
    it would be uploaded to company cloud storage.

    python longmemeval_m_fetch.py            # download (resumes) and split
    python longmemeval_m_fetch.py --check    # report what is already on disk
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

URL = ("https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
       "longmemeval_m_cleaned.json")
HOME = Path.home() / "longmemeval-m"        # deliberately not under OneDrive
RAW = HOME / "longmemeval_m_cleaned.json"
SPLIT = HOME / "questions"
EXPECTED_BYTES = 2_737_100_077


def download() -> None:
    """Resumable byte-range download; the connection has dropped on files this size before."""
    have = RAW.stat().st_size if RAW.exists() else 0
    if have >= EXPECTED_BYTES:
        print(f"already downloaded: {have:,} bytes")
        return
    headers = {"Range": f"bytes={have}-"} if have else {}
    print(f"downloading from byte {have:,} of {EXPECTED_BYTES:,}")
    with urlopen(Request(URL, headers=headers), timeout=300) as response, \
            RAW.open("ab" if have else "wb") as out:
        done = have
        while chunk := response.read(8 << 20):
            out.write(chunk)
            done += len(chunk)
            print(f"\r  {done / EXPECTED_BYTES:6.1%}  {done / 1e9:5.2f} GB", end="", flush=True)
    print(f"\ndone: {RAW.stat().st_size:,} bytes")


def stream_instances(path: Path):
    """Yield one instance at a time from a JSON array too large to parse whole.

    The array is scanned character by character, tracking string state and brace depth, so an object
    is only ever materialised one at a time. No JSON library can do this without ijson, which is not
    a dependency here.
    """
    with path.open(encoding="utf-8") as stream:
        depth, buffer, in_string, escaped = 0, [], False, False
        while block := stream.read(1 << 20):
            for char in block:
                if depth:
                    buffer.append(char)
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    if not depth:
                        buffer = ["{"]
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if not depth:
                        yield json.loads("".join(buffer))
                        buffer = []


def split(limit: int | None = None) -> dict:
    SPLIT.mkdir(parents=True, exist_ok=True)
    index, sessions = [], 0
    for instance in stream_instances(RAW):
        path = SPLIT / f"{instance['question_id']}.json"
        path.write_text(json.dumps(instance, ensure_ascii=False), encoding="utf-8")
        index.append({"question_id": instance["question_id"],
                      "question_type": instance["question_type"],
                      "sessions": len(instance["haystack_session_ids"]),
                      "answer_sessions": len(instance["answer_session_ids"]),
                      "bytes": path.stat().st_size})
        sessions += len(instance["haystack_session_ids"])
        if len(index) % 25 == 0:
            print(f"\r  {len(index)} questions, {sessions:,} sessions", end="", flush=True)
        if limit and len(index) >= limit:
            break
    report = {"questions": len(index), "sessions": sessions,
              "sessions_per_question": round(sessions / max(1, len(index)), 1),
              "questions_index": index}
    (HOME / "index.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\n{len(index)} questions split, {sessions:,} sessions, "
          f"{report['sessions_per_question']} per question")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    HOME.mkdir(parents=True, exist_ok=True)
    if args.check:
        have = RAW.stat().st_size if RAW.exists() else 0
        done = len(list(SPLIT.glob("*.json"))) if SPLIT.exists() else 0
        print(f"raw {have:,} / {EXPECTED_BYTES:,} bytes, {done} questions split")
        return
    download()
    split(args.limit)


if __name__ == "__main__":
    main()
