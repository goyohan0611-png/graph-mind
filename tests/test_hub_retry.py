"""Postgres's tools fail at random on some Windows PCs (AhnLab V3): hub setup tries again."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import subprocess
import unittest
from unittest import mock

from graph_mind import sharing


class HubRetryTests(unittest.TestCase):
    def test_a_flaky_step_is_tried_again_and_cleaned_up_between(self):
        calls, undone = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise subprocess.CalledProcessError(1, "initdb")
            return "ok"

        with mock.patch("time.sleep"):
            self.assertEqual(sharing._retry(flaky, attempts=10, undo=lambda: undone.append(1)),
                             "ok")
        self.assertEqual((len(calls), len(undone)), (3, 2))

    def test_gives_up_after_the_last_attempt(self):
        def broken():
            raise subprocess.CalledProcessError(1, "pg_ctl")
        with mock.patch("time.sleep"), self.assertRaises(subprocess.CalledProcessError):
            sharing._retry(broken, attempts=3)

    def test_a_start_that_failed_after_initdb_still_gets_the_memory_database(self):
        """The first start fails (as cmd.exe denials do) after initdb made the cluster: the next
        call starts it and still creates the role and database the memory lives in."""
        from pathlib import Path
        import os
        import tempfile
        import time

        import psycopg
        real, tries = sharing._start, []

        def first_fails(data, port):
            tries.append(1)
            if len(tries) == 1:
                raise subprocess.CalledProcessError(1, "pg_ctl")
            real(data, port)
        # not mkdtemp: on Windows its folder is the owner's alone, and initdb run by an
        # administrator (CI) drops to a restricted token that cannot enter it
        home = Path(tempfile.gettempdir()) / f"graph-mind-retry-{os.getpid()}-{time.time_ns()}"
        home.mkdir()
        with mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": str(home)}), \
                mock.patch.object(sharing, "PORT", 54395), \
                mock.patch.object(sharing, "_start", first_fails):
            try:
                with self.assertRaises(subprocess.CalledProcessError):
                    sharing.local_postgres()
                with psycopg.connect(sharing.local_postgres(), connect_timeout=10) as db:
                    self.assertEqual(db.execute("SELECT current_user").fetchone()[0], "graphmind")
            finally:
                sharing.stop_postgres()

    def test_the_hubs_postgres_is_fetched_and_speaks_tls(self):
        """Any Python, any supported machine: the pinned build is fetched, runs, has OpenSSL."""
        self.assertIn("PostgreSQL) 16.", sharing._pg_tool("postgres", ["-V"]))
        self.assertIn("openssl", sharing._pg_tool("pg_config", ["--configure"]))


if __name__ == "__main__":
    unittest.main()
