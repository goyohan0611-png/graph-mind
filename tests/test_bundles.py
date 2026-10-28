"""Work bundles: one project folder's conversations listed, deleted with a backup, restored."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import os
import tempfile
import unittest

from graph_mind import bundles
from graph_mind.conversation_memory import ConversationMemoryStore


def turn(i, cwd, client="claude-code", text=None):
    return {"turn_id": f"t{i}", "client": client, "client_session_id": f"s-{client}",
            "scope": "global", "role": "user", "cwd": cwd,
            "content_redacted": text or f"turn {i} about {cwd}", "raw_sha256": f"x{i}",
            "source_path": "p", "happened_at": f"2026-10-0{1 + i % 9}T09:00:00",
            "known_at": "2026-10-01T09:00:00", "source_ordinal": i}


class BundleTests(unittest.TestCase):
    def test_delete_one_folder_with_a_backup_then_restore_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, other = str(root / "work"), str(root / "other")
            index = root / "m.sqlite"
            with ConversationMemoryStore(index) as store:
                for i, (cwd, client) in enumerate([(work, "claude-code"), (work, "codex"),
                                                   (os.path.join(work, "sub"), "codex"),
                                                   (other, "codex"), (None, "claude-desktop")]):
                    store.record(turn(i, cwd, client))
            listed = {b["bundle"]: b for b in bundles.list_bundles(index)}
            self.assertEqual(listed["app:claude-desktop"]["turns"], 1)

            planned = bundles.plan(index, work)
            self.assertEqual((planned["turns"], planned["apps"]), (3, ["claude-code", "codex"]))
            refused = bundles.delete(index, work, "wrong", backups=root / "b")
            self.assertEqual(refused["status"], "CHANGED_SINCE_PLANNED")

            done = bundles.delete(index, work, planned["token"], backups=root / "b")
            self.assertEqual((done["status"], done["turns"]), ("DELETED", 3))
            with ConversationMemoryStore(index) as store:
                left = sorted(r[0] for r in store.db.execute(
                    "SELECT turn_id FROM conversation_turns"))
                self.assertEqual(left, ["t3", "t4"])
                store.record(turn(0, work))            # forgotten stays forgotten
                self.assertEqual(store.db.execute(
                    "SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 2)

            bundles.restore(index, done["backup"])
            with ConversationMemoryStore(index) as store:
                back = sorted(r[0] for r in store.db.execute(
                    "SELECT turn_id FROM conversation_turns"))
            self.assertEqual(back, ["t0", "t1", "t2", "t3", "t4"])


if __name__ == "__main__":
    unittest.main()
