"""Does Graph-MIND heal by itself? Every fault it is meant to survive, run and timed.

Each scenario is a test that breaks something the way it breaks on users' PCs and checks the
memory keeps answering or comes back without the user. The gaps below are faults nothing handles
yet; they are listed so the report never reads as more complete than it is.

    python bench/recovery.py
"""
from __future__ import annotations

from pathlib import Path
import os
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

SCENARIOS = [
    ("hub Postgres killed; the next use starts it again, data intact",
     "test_recovery.HubProcessDiedTests.test_the_next_use_starts_a_killed_postgres_again"),
    ("first start fails right after the cluster is made",
     "test_hub_retry.HubRetryTests."
     "test_a_start_that_failed_after_initdb_still_gets_the_memory_database"),
    ("Postgres tools refused at random (antivirus blocking cmd.exe)",
     "test_hub_retry.HubRetryTests.test_a_flaky_step_is_tried_again_and_cleaned_up_between"),
    ("shared hub unreachable; new lines wait and go later",
     "test_shared_brain.OutboxTests."
     "test_lines_wait_while_the_hub_is_down_and_go_with_the_next_append"),
    ("an app changes its transcript format; the user is told",
     "test_capture_health.StallWarningTests."
     "test_rows_without_turns_add_up_across_passes_then_warn"),
    ("embedding model still loading; word search answers at once",
     "test_cold_start.ModelStillLoadingTests.test_a_question_does_not_wait_for_the_model"),
    ("embedding model cannot run; word search still answers",
     "test_recovery.ModelBrokenTests.test_word_search_still_answers_when_the_model_cannot_run"),
    ("three apps write the same store at once",
     "test_concurrent_use.ConcurrentUseTests.test_three_writers_one_store"),
]
GAPS = [
    "the capture service dies: nothing restarts it before the next login, and nothing is saved "
    "meanwhile",
]


def main() -> int:
    import _isolate  # noqa: F401  (never touch the real ~/.graph-mind)
    loader, failed = unittest.TestLoader(), 0
    print(f"{'scenario':<66}{'result':>8}{'time':>8}")
    for title, name in SCENARIOS:
        began = time.perf_counter()
        try:
            result = unittest.TextTestRunner(stream=open(os.devnull, "w"),
                                             verbosity=0).run(loader.loadTestsFromName(name))
            passed = result.wasSuccessful()
        except Exception:                        # a scenario that no longer exists fails too
            passed = False
        failed += not passed
        print(f"{title:<66}{'healed' if passed else 'FAILED':>8}"
              f"{time.perf_counter() - began:>7.0f}s")
    print("\nnot handled yet:")
    for gap in GAPS:
        print(f"  - {gap}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
