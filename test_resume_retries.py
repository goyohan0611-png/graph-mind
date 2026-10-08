"""A resumed run must retry what failed. It used to count any written row as done: 640 extractions
that failed with a revoked key were skipped for good and the answers were built on nothing."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import json
import tempfile
import unittest

import longmemeval_retrieval_executor as pipe


class ResumeRetryTests(unittest.TestCase):
    def test_failed_rows_are_pending_and_a_later_success_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ingestion.jsonl"
            rows = [{"custom_id": "a", "status": "transport_error", "http_status": 401},
                    {"custom_id": "b", "status": "ok"},
                    {"custom_id": "c", "status": "parse_failure"},
                    {"custom_id": "c", "status": "ok"},          # retried later and succeeded
                    {"custom_id": "d"}]                          # older files carry no status
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            self.assertEqual(pipe.finished(pipe.load_jsonl(path, "custom_id")), {"b", "c", "d"})


if __name__ == "__main__":
    unittest.main()
