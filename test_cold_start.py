"""The first question after a busy day: answered by meaning, not by word search alone.

A memory server that has been open since morning holds the vectors it loaded then. Meanwhile the
capture service stores a day of turns. Before, the server met hundreds of unembedded pieces, sent
them to a background fill and searched none of them by meaning. Now the service embeds what it
stores, and the server picks those vectors up from the shared cache before it searches.
"""
from pathlib import Path
from unittest import mock
import tempfile
import unittest

import graph_mind_mcp_server as server
from conversation_memory import ConversationMemoryStore
from embedding_warmup import warm_turns
from semantic_recall import default_embedder

FILLER = ["오늘 회의에서 분기 예산 이야기를 길게 했다", "점심 메뉴 고르느라 시간이 걸렸다",
          "빌드가 깨져서 의존성 버전을 다시 맞췄다", "주말에 읽을 책 목록을 정리했다",
          "운동 루틴을 바꿔볼까 고민 중이다", "프린터가 또 용지 걸림을 일으켰다"]
TARGET = "우리 강아지 콩이는 파도 소리만 들어도 벌벌 떨면서 바닷가에 안 가려고 해"


def turn(i, text):
    when = f"2026-10-08T{9 + i // 60:02d}:{i % 60:02d}:00"
    return {"turn_id": f"t{i}", "client": "claude-code", "client_session_id": "s",
            "scope": "global", "role": "user", "content_redacted": text, "raw_sha256": f"t{i}",
            "source_path": "p", "happened_at": when, "known_at": when, "source_ordinal": i}


class ColdStartTests(unittest.TestCase):
    def test_turns_captured_after_the_server_started_are_searched_by_meaning(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            open_since_morning = default_embedder(db)          # the memory server's view
            with ConversationMemoryStore(db) as store:            # the day's capture
                for i in range(150):
                    store.record(turn(i, f"{FILLER[i % len(FILLER)]} (메모 {i})"))
                store.record(turn(150, TARGET))
            question = "반려견이 무서워하는 장소가 어디였지?"   # shares no word with the turn

            with mock.patch.object(server, "embed_in_background"):
                cold = server.semantic_turns(db, question, embedder=open_since_morning)
            self.assertEqual(cold, [], "the old behaviour: nothing searched by meaning")

            self.assertEqual(warm_turns(db, embedder=default_embedder(db)), 151)   # the service

            with mock.patch.object(server, "embed_in_background",
                                   side_effect=AssertionError("no backlog left to fill")):
                warm = server.semantic_turns(db, question, embedder=open_since_morning, limit=3)
            self.assertIn("t150", [hit["turn_id"] for hit in warm])


if __name__ == "__main__":
    unittest.main()
