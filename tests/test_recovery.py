"""Faults that must heal without the user: a hub process that died, an embedding model that will
not run. bench/recovery.py runs these with the other fault tests and reports them together."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import os
import tempfile
import time
import unittest

from graph_mind import mcp_server as server
from graph_mind import sharing
from graph_mind.conversation_memory import ConversationMemoryStore


class HubProcessDiedTests(unittest.TestCase):
    def test_the_next_use_starts_a_killed_postgres_again(self):
        """A crash, an update killing it, a console closed on it: whoever needs the memory next
        starts it again, and what was written before is still there."""
        import psutil
        import psycopg
        # not mkdtemp: initdb run by an administrator (CI) cannot enter its owner-only folder
        home = Path(tempfile.gettempdir()) / f"graph-mind-recovery-{os.getpid()}-{time.time_ns()}"
        home.mkdir()
        with mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": str(home)}), \
                mock.patch.object(sharing, "PORT", 54396):
            try:
                with psycopg.connect(sharing.local_postgres(), autocommit=True) as db:
                    db.execute("CREATE TABLE kept(note text)")
                    db.execute("INSERT INTO kept VALUES ('before the crash')")
                pid = (sharing._pg_data() / "postmaster.pid").read_text(encoding="utf-8")
                postmaster = psutil.Process(int(pid.split()[0]))
                for process in [*postmaster.children(recursive=True), postmaster]:
                    process.kill()                          # the whole server, no clean stop
                time.sleep(1)
                self.assertIsNone(sharing._server_exe(sharing._pg_data()))
                with psycopg.connect(sharing.local_postgres(), connect_timeout=10) as db:
                    self.assertEqual(db.execute("SELECT note FROM kept").fetchone()[0],
                                     "before the crash")
            finally:
                sharing.stop_postgres()


class ModelBrokenTests(unittest.TestCase):
    def test_word_search_still_answers_when_the_model_cannot_run(self):
        """A model download cut short, a runtime DLL missing: recall answers by words."""
        class Broken:
            def embed(self, texts):
                raise OSError("model file is truncated")

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record({"turn_id": "t1", "client": "claude-code", "client_session_id": "s",
                              "scope": "global", "role": "user",
                              "content_redacted": "the invoice for October is due on the 25th",
                              "raw_sha256": "t1", "source_path": "p", "source_ordinal": 1,
                              "happened_at": "2026-10-01T09:00:00",
                              "known_at": "2026-10-01T09:00:00"})
            recalled = server.gather(db, "when is the October invoice due",
                                     as_of="2026-10-14T00:00:00", embedder=Broken())
        self.assertIn("t1", [t["turn_id"] for t in recalled["conversation_turns"]])


if __name__ == "__main__":
    unittest.main()
