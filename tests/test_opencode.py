import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
import json
from pathlib import Path
import tempfile
import unittest

from graph_mind.conversation_memory import ConversationMemoryStore, OpenCodeConversationCapture
from graph_mind.inject import context


class OpenCodeTests(unittest.TestCase):
    def test_what_the_plugin_writes_is_captured_once(self):
        """The plugin's transcript (Claude Code's record shape): both sides, stored once."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            records = [
                {"type": "user", "uuid": "m1", "sessionId": "ses_1", "cwd": directory,
                 "timestamp": "2026-10-16T05:00:00.000Z",
                 "message": {"content": "our golden retriever Kongi turned three"}},
                {"type": "assistant", "uuid": "m2", "sessionId": "ses_1", "cwd": directory,
                 "timestamp": "2026-10-16T05:00:09.000Z",
                 "message": {"content": "Happy birthday to Kongi!"}}]
            (root / "ses_1.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = OpenCodeConversationCapture(
                    root, store, state_path=Path(directory) / "cursor.json")
                capture.initialize_baseline()        # the service seeing the folder first
                capture.scan()
                capture.scan()                                   # nothing new: nothing twice
                rows = store.db.execute("SELECT client, role, content_redacted FROM "
                                        "conversation_turns ORDER BY happened_at").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("opencode", "user", "our golden retriever Kongi turned three"),
                          ("opencode", "assistant", "Happy birthday to Kongi!")])

    def test_a_message_with_no_sign_of_the_past_gets_nothing(self):
        self.assertEqual(context("write a quicksort in rust"), "")
        self.assertEqual(context("   "), "")


if __name__ == "__main__":
    unittest.main()
