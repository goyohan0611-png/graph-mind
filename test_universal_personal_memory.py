import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import tempfile
import unittest

from local_brain import LocalBrainStore
from universal_personal_memory import build_context_pack, decide_recall


T1 = "2026-09-16T10:00:00"


def memory(memory_id="m1", **extra):
    value = {"memory_id": memory_id, "scope": "global", "memory_type": "PREFERENCE",
        "title": "response style", "content": "The user prefers concise explanations.",
        "effective_at": T1, "known_at": T1, "actor": "user", "tags": ["style"],
        "entities": ["user"],
        "provenance": {"source_type": "conversation", "source_ref": "turn:1"},
        "supersedes_memory_id": None}
    value.update(extra)
    return value


class UniversalPersonalMemoryTests(unittest.TestCase):
    def test_profile_round_trip_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with LocalBrainStore(path) as store:
                store.remember(memory(memory_class="PREFERENCE", modality="CONVERSATION",
                    retention="PINNED", importance=0.9, confidence=0.95,
                    attributes={"language": "ko"}, related_memory_ids=["turn:1"]))
                store.remember(memory("legacy", memory_type="NOTE", title="legacy note"))
                result = store.recall("response style legacy note", as_of=T1)
            by_id = {item["memory_id"]: item for item in result["matches"]}
            self.assertEqual(by_id["m1"]["retention"], "PINNED")
            self.assertEqual(by_id["m1"]["attributes"], {"language": "ko"})
            self.assertEqual(by_id["legacy"]["memory_class"], "EPISODE")
            self.assertEqual(by_id["legacy"]["retention"], "EPISODIC")

    def test_profile_is_idempotent_but_rejects_changed_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            item = memory(importance=0.8)
            with LocalBrainStore(path) as store:
                store.remember(item)
                self.assertEqual(store.remember(item)["disposition"], "ALREADY_RECORDED")
                changed = dict(item, importance=0.7)
                with self.assertRaisesRegex(ValueError,
                                            "UNIVERSAL_MEMORY_PROFILE_COLLISION"):
                    store.remember(changed)

    def test_recall_router_separates_general_and_personal_questions(self):
        self.assertEqual(decide_recall("What is photosynthesis?")["mode"], "SKIP")
        self.assertEqual(decide_recall("어제 작성하던 문서가 뭐였지?")["mode"], "SEARCH")
        self.assertEqual(decide_recall("지난달에 한 번도 안 갔어?")["mode"], "EXECUTE")
        self.assertEqual(decide_recall("anything", "always")["mode"], "SEARCH")

    def test_context_pack_is_bounded_and_source_labelled(self):
        recalled = {"scope_completeness": "UNATTESTED", "matches": [{
            "memory_id": "m1", "scope": "global", "memory_class": "FACT",
            "modality": "DOCUMENT", "title": "long note", "content": "x" * 1000,
            "effective_at": T1, "known_at": T1,
            "provenance": {"source_type": "document", "source_ref": "note.md"},
            "confidence": 1.0, "importance": 0.8}], "conversation_turns": []}
        packet = build_context_pack(recalled, max_chars=256)
        self.assertEqual(packet["status"], "KNOWN")
        self.assertLessEqual(packet["used_chars"], 256)
        self.assertIn("Source: document / note.md", packet["context"])
        self.assertTrue(packet["items"][0]["truncated"])


if __name__ == "__main__":
    unittest.main()
