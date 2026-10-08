"""Forget everything that contains a phrase: on this PC, in the shared log, and on the other PCs.

    graph-mind-forget                  # asks for the phrase without echoing it (keeps it out of
                                       # the shell history), shows what matches, asks to confirm
    graph-mind-forget "hunter2" --yes

A captured turn or memory lives in its store, in that store's word index, and as an engram in the
associative index; all three go. In the shared log its lines go too: Postgres rows are deleted, a
folder's lines are blanked in place (same length, so other PCs' read offsets stay valid). Then a
"forget" record listing the ids, never the phrase, tells every other PC to erase its own copies.

Not covered: the AI app's own transcript (Claude Code keeps ~/.claude/projects/*.jsonl) and the
embedding vectors, which are numbers keyed by a hash and hold no text.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import getpass
import json
import sqlite3
import sys


def find(index: Path, phrase: str) -> dict:
    """Ids of the turns and memories whose text contains the phrase (exact, case-sensitive)."""
    db = sqlite3.connect(index, timeout=30)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        turns = [row[0] for row in db.execute(
            "SELECT turn_id FROM conversation_turns WHERE instr(content_redacted, ?) > 0",
            (phrase,))] if "conversation_turns" in tables else []
        memories = [row[0] for row in db.execute(
            "SELECT memory_id FROM local_brain_memories "
            "WHERE instr(content, ?) > 0 OR instr(title, ?) > 0",
            (phrase, phrase))] if "local_brain_memories" in tables else []
    finally:
        db.close()
    return {"turn_ids": turns, "memory_ids": memories}


def erase(index: Path, ids: dict) -> dict:
    """Delete these turns and memories, with every index entry derived from them."""
    turns, memories = list(ids.get("turn_ids", [])), list(ids.get("memory_ids", []))
    db = sqlite3.connect(index, timeout=30)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        with db:
            if turns and "conversation_turns" in tables:
                marks = ",".join("?" * len(turns))
                rows = db.execute(f"SELECT revision, content_redacted FROM conversation_turns "
                                  f"WHERE turn_id IN ({marks})", turns).fetchall()
                # an external-content FTS table keeps its words until told to drop them
                db.executemany("INSERT INTO conversation_turns_fts(conversation_turns_fts, rowid, "
                               "content_redacted) VALUES('delete', ?, ?)", rows)
                db.execute(f"DELETE FROM conversation_turns WHERE turn_id IN ({marks})", turns)
            if memories and "local_brain_memories" in tables:
                marks = ",".join("?" * len(memories))
                db.execute(f"DELETE FROM local_brain_memories WHERE memory_id IN ({marks})",
                           memories)
                if "local_brain_memory_profiles" in tables:
                    db.execute(f"DELETE FROM local_brain_memory_profiles "
                               f"WHERE memory_id IN ({marks})", memories)
            sources = turns + memories
            if sources and "associative_engrams" in tables:
                marks = ",".join("?" * len(sources))
                engrams = [row[0] for row in db.execute(
                    f"SELECT engram_id FROM associative_engrams WHERE source_id IN ({marks})",
                    sources)]
                if engrams:
                    em = ",".join("?" * len(engrams))
                    for table, column in (("associative_engrams_fts", "engram_id"),
                                          ("associative_terms", "engram_id"),
                                          ("associative_edges", "source_engram_id"),
                                          ("associative_edges", "target_engram_id"),
                                          ("associative_pending_edges", "source_engram_id"),
                                          ("associative_pending_edges", "target_engram_id"),
                                          ("associative_engrams", "engram_id")):
                        if table in tables:
                            db.execute(f"DELETE FROM {table} WHERE {column} IN ({em})", engrams)
    finally:
        db.close()
    return {"turns": len(turns), "memories": len(memories)}


def _keys(ids: dict) -> list[str]:
    """How a target's log line spells its id (brain_log writes sorted keys, ": " separators)."""
    return ([f'"turn_id": {json.dumps(t)}' for t in ids.get("turn_ids", [])]
            + [f'"memory_id": {json.dumps(m)}' for m in ids.get("memory_ids", [])])


def scrub_shared(shared, ids: dict) -> int:
    """Remove the targets' lines from the shared log. Returns how many lines went."""
    from brain_log import _pg, is_database
    keys = _keys(ids)
    if not keys:
        return 0
    if is_database(shared):
        with _pg(shared) as connection:
            removed = 0
            for key in keys:
                removed += connection.execute(
                    "DELETE FROM brain_log WHERE strpos(line, %s) > 0", (key,)).rowcount
            connection.commit()
        return removed
    removed = 0
    for log in (Path(shared) / "log").glob("*.jsonl"):
        data = log.read_bytes()
        lines = data.split(b"\n")
        changed = False
        for n, line in enumerate(lines):
            text = line.decode("utf-8", "replace")
            if any(key in text for key in keys) and '"kind": "forget"' not in text:
                lines[n] = b" " * len(line)        # same length: readers' offsets stay valid
                changed, removed = True, removed + 1
        if changed:
            with log.open("r+b") as stream:            # in place: the file is never shorter
                stream.write(b"\n".join(lines))
    return removed


def forget(phrase: str, index: Path, shared=None) -> dict:
    """Find, erase here, scrub the shared log, and tell the other PCs."""
    from brain_log import append
    ids = find(index, phrase)
    if not (ids["turn_ids"] or ids["memory_ids"]):
        return {"turns": 0, "memories": 0, "shared_lines_removed": 0}
    result = erase(index, ids)
    if shared:
        result["shared_lines_removed"] = scrub_shared(shared, ids)
        append(shared, "forget", ids)                  # ids only: the phrase never leaves here
    return result


def main(argv=None):
    from brain_log import folder
    from development_paths import default_development_db
    parser = argparse.ArgumentParser(description="Forget every captured turn and memory "
                                                 "that contains a phrase, on every PC.")
    parser.add_argument("phrase", nargs="?", help="omit it to type it without echo")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    phrase = args.phrase or getpass.getpass("Phrase to forget (not shown): ")
    if len(phrase.strip()) < 4:
        sys.exit("Use at least 4 characters, so a short word does not wipe half the memory.")
    index = default_development_db()
    ids = find(index, phrase)
    count = len(ids["turn_ids"]) + len(ids["memory_ids"])
    if not count:
        print("Nothing in this PC's memory contains that phrase.")
        return
    print(f"{len(ids['turn_ids'])} conversation turns and {len(ids['memory_ids'])} memories "
          "contain it.")
    if not args.yes and input("Forget them here and on your other PCs? [y/N] ").lower() != "y":
        print("Nothing changed.")
        return
    shared = folder()
    result = forget(phrase, index, shared)
    print(f"Forgotten: {result['turns']} turns, {result['memories']} memories"
          + (f"; {result['shared_lines_removed']} lines removed from the shared memory, "
             "other PCs follow on their next sync" if shared else "") + ".")
    print("Your AI app may still have it in its own history (Claude Code: ~/.claude/projects).")


if __name__ == "__main__":
    main()
