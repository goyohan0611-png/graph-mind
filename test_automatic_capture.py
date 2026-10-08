from pathlib import Path
import json
import tempfile
import unittest

from automatic_capture import AutomaticCaptureService, make_config
from coding_memory import CodingMemoryStore
from conversation_memory import ConversationMemoryStore


class AutomaticCaptureTests(unittest.TestCase):
    def test_baseline_then_incrementally_captures_code_and_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); workspace = root / "workspace"
            sessions = root / "sessions"; state = root / "state"
            workspace.mkdir(); sessions.mkdir()
            code = workspace / "app.py"; code.write_text("def value():\n    return 1\n")
            rollout = sessions / "rollout.jsonl"
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {
                "session_id": "test-session", "cwd": str(workspace)}}) + "\n")
            config = make_config(workspace=workspace, project_id="project:test",
                session_id="capture:test", sessions_root=sessions,
                db_path=root / "brain.sqlite", poll_seconds=1)
            config["code_workspaces"][0]["state_dir"] = str(state / "coding")
            service = AutomaticCaptureService(config, state_dir=state,
                                               now=lambda: "2026-09-16T12:00:00")
            baseline = service.baseline()
            code.write_text("def value():\n    return 2\n")
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": "2026-09-16T03:00:00Z",
                    "ordinal": 1, "type": "response_item", "payload": {
                        "type": "message", "role": "user", "content": [{
                            "type": "input_text", "text": "프로젝트 값을 바꿨어"}]}}) + "\n")
            first = service.run_once(); second = service.run_once()
            with CodingMemoryStore(root / "brain.sqlite") as store:
                changed = store.query(project_id="project:test", file_path="app.py")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                recalled = store.search("프로젝트 값", scopes=["project:test"])
            self.assertEqual(baseline["status"], "BASELINE_READY")
            self.assertEqual(first["status"], "CAPTURE_OK")
            self.assertEqual(first["semantic_llm"]["status"], "DISABLED")
            self.assertEqual(first["coding"][0]["change_count"], 1)
            self.assertEqual(first["conversation"]["recorded_turns"], 1)
            self.assertEqual(second["coding"][0]["status"], "NO_CHANGES")
            self.assertEqual(second["conversation"]["status"], "NO_NEW_TURNS")
            self.assertEqual({item["symbol_name"] for item in
                              changed["changes"][0]["symbols"]}, {"value"})
            self.assertEqual(recalled["status"], "KNOWN")
            status = json.loads((state / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "CAPTURE_OK")

    def test_conversation_cycle_can_skip_expensive_code_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); workspace = root / "workspace"
            sessions = root / "sessions"; workspace.mkdir(); sessions.mkdir()
            (workspace / "app.py").write_text("x = 1\n")
            (sessions / "rollout.jsonl").write_text("", encoding="utf-8")
            config = make_config(workspace=workspace, project_id="project:test",
                session_id="capture:test", sessions_root=sessions,
                db_path=root / "brain.sqlite", poll_seconds=1,
                code_poll_seconds=30)
            config["code_workspaces"][0]["state_dir"] = str(root / "coding")
            service = AutomaticCaptureService(config, state_dir=root / "state",
                                               now=lambda: "2026-09-16T12:00:00")
            service.baseline()
            (workspace / "app.py").write_text("x = 2\n")
            skipped = service.run_once(capture_code=False)
            captured = service.run_once(capture_code=True)
            self.assertEqual(skipped["coding"][0]["status"], "SKIPPED_NOT_DUE")
            self.assertEqual(captured["coding"][0]["change_count"], 1)


if __name__ == "__main__":
    unittest.main()
