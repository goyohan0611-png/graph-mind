import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import tempfile
import unittest

from coding_memory import CodingMemoryCapture, CodingMemoryStore


T1 = "2026-09-15T10:00:00"


class CodingMemoryTests(unittest.TestCase):
    def test_snapshot_diff_symbols_and_indexed_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state = root / "workspace", root / "state"
            workspace.mkdir()
            source = workspace / "app.py"
            source.write_text("def greet(name):\n    return 'hi ' + name\n", encoding="utf-8")
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store,
                    project_id="project:demo", session_id="session:1",
                    state_dir=state, now=lambda: T1)
                baseline = capture.initialize_baseline()
                self.assertEqual(baseline["file_count"], 1)
                source.write_text("def greet(name):\n    return f'hello {name}'\n\n"
                                  "def farewell(name):\n    return f'bye {name}'\n",
                                  encoding="utf-8")
                result = capture.scan(reason="Improve greeting",
                                      related_tests=["test_greet"])
                self.assertEqual(result["status"], "CHANGES_RECORDED")
                self.assertEqual(result["change_count"], 1)
                self.assertIn("greet", result["changes"][0]["symbols"])
                self.assertIn("farewell", result["changes"][0]["symbols"])
                self.assertIn("hello", Path(result["changes"][0]["diff_ref"])
                              .read_text(encoding="utf-8"))
                queried = store.query(project_id="project:demo", symbol="greet",
                                      happened_from="2026-09-15T00:00:00+09:00",
                                      happened_to="2026-09-15T23:59:59+09:00")
                self.assertEqual(queried["status"], "KNOWN")
                self.assertEqual(queried["changes"][0]["file_path"], "app.py")
                self.assertEqual(queried["changes"][0]["reason"], "Improve greeting")
                self.assertEqual(queried["changes"][0]["related_tests"], ["test_greet"])
                self.assertIn("hello", queried["changes"][0]["diff_excerpt"])
                self.assertFalse(queried["changes"][0]["diff_truncated"])
                self.assertEqual(capture.scan(reason="No new change")["status"],
                                 "NO_CHANGES")

    def test_project_file_action_filters_and_unknown_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state = root / "workspace", root / "state"
            workspace.mkdir()
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store,
                    project_id="project:one", session_id="session:1",
                    state_dir=state, now=lambda: T1)
                capture.initialize_baseline()
                (workspace / "new.py").write_text("class Added:\n    pass\n", encoding="utf-8")
                capture.scan(reason="Add class")
                (workspace / "test_new.py").write_text("def test_added():\n    pass\n",
                                                       encoding="utf-8")
                capture.scan(reason="Add test")
                found = store.query(project_id="project:one", file_path="new.py",
                                    change_kind="ADDED")
                missing = store.query(project_id="project:two")
            self.assertEqual(found["changes"][0]["change_kind"], "ADDED")
            self.assertEqual(found["changes"][0]["file_path"], "new.py")
            self.assertEqual(found["changes"][0]["symbols"][0]["symbol_name"], "Added")
            self.assertEqual((missing["status"], missing["reason"]),
                             ("UNKNOWN", "NO_MATCHING_CODE_CHANGE"))


if __name__ == "__main__":
    unittest.main()
