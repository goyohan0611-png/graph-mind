"""Postgres's tools fail at random on some Windows PCs (AhnLab V3): hub setup tries again."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import subprocess
import unittest
from unittest import mock

import brain_log


class HubRetryTests(unittest.TestCase):
    def test_a_flaky_step_is_tried_again_and_cleaned_up_between(self):
        calls, undone = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise subprocess.CalledProcessError(1, "initdb")
            return "ok"

        with mock.patch("time.sleep"):
            self.assertEqual(brain_log._retry(flaky, attempts=10, undo=lambda: undone.append(1)),
                             "ok")
        self.assertEqual((len(calls), len(undone)), (3, 2))

    def test_gives_up_after_the_last_attempt(self):
        def broken():
            raise subprocess.CalledProcessError(1, "pg_ctl")
        with mock.patch("time.sleep"), self.assertRaises(subprocess.CalledProcessError):
            brain_log._retry(broken, attempts=3)

    def test_the_hubs_postgres_is_fetched_and_speaks_tls(self):
        """Any Python, any supported machine: the pinned build is fetched, runs, has OpenSSL."""
        self.assertIn("PostgreSQL) 16.", brain_log._pg_tool("postgres", ["-V"]))
        self.assertIn("openssl", brain_log._pg_tool("pg_config", ["--configure"]))


if __name__ == "__main__":
    unittest.main()
