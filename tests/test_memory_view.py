"""graph-mind-view draws the association graph; with --no-text the page holds no conversation."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import tempfile
import unittest

from graph_mind import memory_view
from graph_mind.associative_memory import AssociativeMemoryIndex
from graph_mind.conversation_memory import ConversationMemoryStore

TEXTS = ["콩이랑 해변 산책 다녀옴", "콩이 사료 바꿨더니 잘 먹네", "서버 배포 스크립트 고쳤다",
         "배포 서버 포트 54329로 고정", "점심은 김치찌개", "콩이 병원 예약 금요일"]


class BrainViewTests(unittest.TestCase):
    def test_graph_page_with_and_without_text(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                for i, text in enumerate(TEXTS):
                    when = f"2026-10-0{1 + i}T10:00:00"
                    store.record({"turn_id": f"t{i}", "client": "codex", "client_session_id": "s",
                                  "scope": "global", "role": "user", "content_redacted": text,
                                  "raw_sha256": f"t{i}", "source_path": "p", "happened_at": when,
                                  "known_at": when, "source_ordinal": i})
            with AssociativeMemoryIndex(db) as index:
                index.sync_sources(limit=1000)
            page = Path(directory) / "brain.html"
            shown = memory_view.build(db, page, inline_library=False)
            self.assertEqual(shown["nodes"], len(TEXTS))
            self.assertGreater(shown["links"], 0)
            self.assertIn("김치찌개", page.read_text(encoding="utf-8"))
            memory_view.build(db, page, with_text=False, inline_library=False)
            html = page.read_text(encoding="utf-8")
            self.assertFalse(any(text in html for text in TEXTS))
            self.assertNotIn("콩이", html)              # nor the topic words of a region
            self.assertNotIn("54329", html)


if __name__ == "__main__":
    unittest.main()
