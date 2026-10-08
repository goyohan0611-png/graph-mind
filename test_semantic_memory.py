from pathlib import Path
import tempfile
import unittest

from conversation_memory import ConversationMemoryStore
from local_brain import LocalBrainStore
from semantic_memory import SemanticMemoryIngestion, classify_durable_turn


T = "2026-09-16T12:00:00"


def turn(turn_id, role, content):
    return {"turn_id": turn_id, "client": "codex", "client_session_id": "s1",
        "scope": "project:test", "cwd": None, "happened_at": T, "known_at": T,
        "role": role, "content_redacted": content, "raw_sha256": turn_id,
        "redaction_count": 0, "source_path": "test.jsonl", "source_ordinal": 1}


class SemanticMemoryTests(unittest.TestCase):
    def test_current_tasks_and_general_questions_are_not_promoted(self):
        for text in ("이 코드 리뷰해줘", "이 에러 수정해줘", "이거 뭐야?",
                     "파이썬 리스트가 뭐야?"):
            category, _ = classify_durable_turn("user", text)
            self.assertIsNone(category)
        self.assertEqual(classify_durable_turn("assistant",
            "앞으로 이걸 사용하기로 했다")[1], "NON_USER_TURN")
        self.assertEqual(classify_durable_turn("user",
            "앞으로는 무조건 기억을 조회하는 거야?")[1],
            "CURRENT_OR_GENERAL_QUESTION")

    def test_explicit_decision_and_preference_are_promoted_with_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn("t1", "user",
                    "앞으로는 Local Brain을 필요한 과거 질문에만 사용하기로 하자"))
                store.record(turn("t2", "user", "이 코드 리뷰해줘"))
                store.record(turn("t3", "user", "나는 짧은 답변을 선호한다"))
                store.record(turn("t4", "assistant", "앞으로 전부 저장하겠습니다"))
            with SemanticMemoryIngestion(db, now=lambda: T) as ingestion:
                first = ingestion.process_new(); second = ingestion.process_new()
            with LocalBrainStore(db) as brain:
                decision = brain.recall("Local Brain 과거 질문", as_of=T)
                preference = brain.recall("짧은 답변 선호", as_of=T)
                count = brain.db.execute("SELECT count(*) FROM local_brain_memories").fetchone()[0]
            with ConversationMemoryStore(db) as conversations:
                small_detail = conversations.search("코드 리뷰")
            self.assertEqual(first["processed_turns"], 4)
            self.assertEqual(first["promoted_count"], 2)
            self.assertEqual(first["evidence_only_count"], 2)
            self.assertEqual(second["status"], "NO_NEW_TURNS")
            self.assertEqual(count, 2)
            self.assertEqual(decision["status"], "KNOWN")
            self.assertEqual(preference["status"], "KNOWN")
            self.assertEqual(small_detail["status"], "KNOWN")
            self.assertEqual(decision["matches"][0]["provenance"]["source_type"],
                             "captured-conversation-turn")

    def test_redacted_content_is_never_automatically_promoted(self):
        category, reason = classify_durable_turn(
            "user", "앞으로 키는 [REDACTED_OPENAI_KEY]로 기억해")
        self.assertIsNone(category)
        self.assertEqual(reason, "REDACTED_CONTENT_REQUIRES_REVIEW")


if __name__ == "__main__":
    unittest.main()
