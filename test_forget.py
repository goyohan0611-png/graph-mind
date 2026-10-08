"""Forget a phrase on one PC: gone from its store and indexes, from the shared log, and from the
other PC after its next sync, and the phrase itself is never written anywhere to make it so."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

import brain_log
from associative_memory import AssociativeMemoryIndex
from conversation_memory import ConversationMemoryStore
from forget import find, forget
from local_brain import LocalBrainStore

SECRET = "hunter2-correct-horse"
T = "2026-10-08T10:00:00"


def turn(n, text):
    return {"turn_id": f"t{n}", "client": "claude-code", "client_session_id": "s", "scope": "global",
            "role": "user", "content_redacted": text, "raw_sha256": f"h{n}", "source_path": "p",
            "happened_at": T, "known_at": T, "source_ordinal": n}


def memory(mid, text):
    return {"memory_id": mid, "scope": "global", "memory_type": "fact", "title": text[:40],
            "content": text, "effective_at": T, "known_at": T, "actor": "assistant", "tags": [],
            "entities": [], "provenance": {"source_type": "test", "source_ref": "forget"},
            "supersedes_memory_id": None}


def words(index, word):
    with ConversationMemoryStore(index) as store:
        return [t["turn_id"] for t in store.search(word, limit=10)["turns"]]


class ForgetTests(unittest.TestCase):
    def test_forgotten_everywhere_and_the_phrase_never_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared, desk, lap = root / "Shared", root / "desk.sqlite", root / "lap.sqlite"
            turns = [turn(1, "the build is green again"),
                     turn(2, f"my wifi password is {SECRET} by the way"),
                     turn(3, "lunch was kimchi stew")]
            secret_memory = memory("m-secret", f"router login {SECRET}")
            try:
                os.environ["GRAPH_MIND_DEVICE"] = "desk"
                with ConversationMemoryStore(desk) as store:
                    for item in turns:
                        store.record(item)
                with LocalBrainStore(desk) as store:
                    store.remember(secret_memory)
                brain_log.append_many(shared, [("turn", t) for t in turns]
                                      + [("memory", secret_memory)])
                with AssociativeMemoryIndex(desk) as index:
                    index.sync_sources()
                os.environ["GRAPH_MIND_DEVICE"] = "lap"
                brain_log.sync(shared, lap)
                self.assertEqual(words(lap, "password"), ["t2"])

                os.environ["GRAPH_MIND_DEVICE"] = "desk"
                result = forget(SECRET, desk, shared)
                self.assertEqual((result["turns"], result["memories"]), (1, 1))
                self.assertEqual(find(desk, SECRET), {"turn_ids": [], "memory_ids": []})
                self.assertEqual(words(desk, "password"), [], "the word index forgot it too")
                self.assertEqual(words(desk, "kimchi"), ["t3"], "nothing else went")
                db = sqlite3.connect(desk)
                engrams = db.execute("SELECT count(*) FROM associative_engrams "
                                     "WHERE source_id IN ('t2','m-secret')").fetchone()[0]
                db.close()
                self.assertEqual(engrams, 0)
                for log in (shared / "log").glob("*.jsonl"):
                    self.assertNotIn(SECRET, log.read_text(encoding="utf-8"))

                os.environ["GRAPH_MIND_DEVICE"] = "lap"
                brain_log.sync(shared, lap)               # the other PC follows on its next sync
                self.assertEqual(find(lap, SECRET), {"turn_ids": [], "memory_ids": []})
                self.assertEqual(words(lap, "password"), [])
                self.assertEqual(sorted(words(lap, "build") + words(lap, "kimchi")), ["t1", "t3"])
            finally:
                os.environ.pop("GRAPH_MIND_DEVICE", None)

    def test_forgotten_from_a_postgres_hub(self):
        import importlib.util
        if importlib.util.find_spec("pgserver") is None:
            self.skipTest("pgserver is built for Python 3.9-3.12 only")
        import pgserver
        import psycopg
        from test_shared_brain import plain_directory
        with plain_directory() as directory:
            root = Path(directory)
            server = pgserver.get_server(root / "pg", cleanup_mode="stop")
            hub, desk, lap = server.get_uri(), root / "desk.sqlite", root / "lap.sqlite"
            try:
                os.environ["GRAPH_MIND_DEVICE"] = "desk"
                items = [turn(1, "the build is green again"), turn(2, f"password {SECRET}")]
                with ConversationMemoryStore(desk) as store:
                    for item in items:
                        store.record(item)
                brain_log.append_many(hub, [("turn", t) for t in items])
                os.environ["GRAPH_MIND_DEVICE"] = "lap"
                brain_log.sync(hub, lap)
                os.environ["GRAPH_MIND_DEVICE"] = "desk"
                forget(SECRET, desk, hub)
                with psycopg.connect(hub) as connection:
                    lines = [row[0] for row in connection.execute("SELECT line FROM brain_log")]
                self.assertFalse(any(SECRET in line for line in lines))
                self.assertTrue(any('"kind": "forget"' in line for line in lines))
                os.environ["GRAPH_MIND_DEVICE"] = "lap"
                brain_log.sync(hub, lap)
                self.assertEqual(find(lap, SECRET), {"turn_ids": [], "memory_ids": []})
                self.assertEqual(words(lap, "build"), ["t1"])
            finally:
                os.environ.pop("GRAPH_MIND_DEVICE", None)
                server.cleanup()

    def test_short_phrases_are_refused(self):
        import forget as module
        with self.assertRaises(SystemExit):
            module.main(["abc", "--yes"])


if __name__ == "__main__":
    unittest.main()
