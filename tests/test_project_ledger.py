"""Shared project checkpoints: published on request, versioned, secret-checked, sealed on a hub,
and never fed from personal memory."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import base64
import importlib.util
import os
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile

from mcp import Client

import project_ledger
from project_ledger import Ledger

KEY = "sk-proj-" + "a" * 40


def ledger(path=None, key=None):
    return Ledger(sqlite3.connect(path or ":memory:"), key=key)


class CheckpointTests(unittest.TestCase):
    def test_publish_needs_an_existing_project(self):
        book = ledger()
        with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
            book.publish("graph-mind", "decision", "x", "yohan")

    def test_timeline_supersede_and_retract(self):
        book = ledger()
        book.create_project("graph-mind", "yohan")
        first = book.publish("graph-mind", "decision", "허브는 PC 한 대", "yohan")
        second = book.publish("graph-mind", "decision", "허브는 PC 두 대로 이중화", "minji",
                              supersedes=first["id"])
        progress = book.publish("graph-mind", "progress", "3D 보기 배포 완료", "yohan",
                                source_refs=["codex-turn-abc", "commit:4211ae2"])
        timeline = book.timeline("graph-mind")
        self.assertEqual([c["id"] for c in timeline], [progress["id"], second["id"], first["id"]])
        self.assertEqual(timeline[2]["status"], "superseded")
        self.assertEqual(timeline[0]["source_refs"], ["codex-turn-abc", "commit:4211ae2"])
        book.retract(progress["id"], "yohan")
        self.assertNotIn(progress["id"], [c["id"] for c in book.timeline("graph-mind")])

    def test_text_is_refused_where_ids_belong_and_secrets_anywhere(self):
        book = ledger()
        book.create_project("p", "a")
        with self.assertRaisesRegex(ValueError, "SOURCE_REFS_ARE_IDS"):
            book.publish("p", "progress", "ok", "a", source_refs=["x" * 300])
        with self.assertRaisesRegex(ValueError, "SECRET_IN_CHECKPOINT"):
            book.publish("p", "progress", f"키는 {KEY}", "a")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.book = ledger()
        self.book.create_project("proposal", "yohan")

    def test_versions_accumulate_and_open_returns_the_exact_bytes(self):
        deck = self.dir / "제안서.pptx"
        with zipfile.ZipFile(deck, "w") as z:
            z.writestr("ppt/slides/slide1.xml", "<p:sp><a:t>Graph-MIND 제안</a:t></p:sp>")
        v1 = self.book.attach("proposal", deck, "제안서 초안 완료", "yohan")
        self.assertEqual(v1["artifacts"][0]["version"], 1)
        self.assertEqual(self.book.attach("proposal", deck, "again", "yohan")["status"],
                         "UNCHANGED")
        with zipfile.ZipFile(deck, "a") as z:
            z.writestr("ppt/slides/slide2.xml", "<a:t>피드백 반영</a:t>")
        v2 = self.book.attach("proposal", deck, "피드백 반영", "minji")
        self.assertEqual(v2["artifacts"][0]["version"], 2)
        got = self.book.fetch("proposal", name="제안서", into=self.dir / "out")
        self.assertEqual((got["version"], got["by"]), (2, "minji"))
        self.assertEqual(Path(got["path"]).read_bytes(), deck.read_bytes())
        old = self.book.fetch("proposal", name="제안서", version=1, into=self.dir / "out")
        self.assertTrue(old["path"].endswith(".v1.pptx"))

    def test_a_key_inside_a_file_or_an_office_document_stops_the_upload(self):
        code = self.dir / "settings.py"
        code.write_text(f"OPENAI_API_KEY = '{KEY}'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SECRET_IN_FILE"):
            self.book.attach("proposal", code, "settings", "yohan")
        deck = self.dir / "deck.pptx"
        with zipfile.ZipFile(deck, "w") as z:
            z.writestr("ppt/slides/slide1.xml", f"<a:t>password: hunter2-very-long</a:t>")
        with self.assertRaisesRegex(ValueError, "SECRET_IN_FILE"):
            self.book.attach("proposal", deck, "deck", "yohan")
        self.assertEqual(self.book.timeline("proposal"), [])

    def test_retract_removes_the_file_from_the_project(self):
        notes = self.dir / "notes.txt"
        notes.write_text("회의록", encoding="utf-8")
        checkpoint = self.book.attach("proposal", notes, "회의록", "yohan")
        self.book.retract(checkpoint["id"], "yohan")
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM ledger_chunks").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "ARTIFACT_NOT_FOUND"):
            self.book.fetch("proposal", name="notes", into=self.dir / "out")

    def test_an_upload_cut_off_halfway_leaves_nothing_behind(self):
        big = self.dir / "cut.bin"
        big.write_bytes(os.urandom(project_ledger.CHUNK * 2 + 10))
        real, calls = self.book._seal, []

        def dies_on_the_second_piece(data):
            calls.append(1)
            if len(calls) == 2:
                raise ConnectionError("network gone")
            return real(data)
        with mock.patch.object(self.book, "_seal", dies_on_the_second_piece), \
                self.assertRaises(ConnectionError):
            self.book.attach("proposal", big, "cut", "yohan")
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM ledger_chunks").fetchone()[0], 0)
        self.assertEqual(self.book.timeline("proposal"), [])

    def test_large_files_go_in_pieces(self):
        big = self.dir / "data.bin"
        big.write_bytes(os.urandom(project_ledger.CHUNK * 2 + 10))
        self.book.attach("proposal", big, "data", "yohan")
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM ledger_chunks").fetchone()[0], 3)
        got = self.book.fetch("proposal", name="data", into=self.dir / "out")
        self.assertEqual(Path(got["path"]).read_bytes(), big.read_bytes())

    def test_a_git_folder_is_linked_by_commit_not_copied(self):
        repo = self.dir / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                        capture_output=True)
        run("init", "-q")
        (repo / "app.py").write_text("print(1)\n")
        run("add", ".")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
        checkpoint = self.book.attach("proposal", repo, None, "yohan")
        link = checkpoint["artifacts"][0]
        self.assertEqual((link["kind"], len(link["commit"])), ("git", 40))
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM ledger_chunks").fetchone()[0], 0)


class SealedTests(unittest.TestCase):
    def test_on_a_hub_neither_summaries_nor_files_are_stored_in_clear(self):
        from cryptography.fernet import Fernet
        path = Path(tempfile.mkdtemp()) / "hub.sqlite"
        book = ledger(path, key=Fernet(base64.urlsafe_b64encode(b"k" * 32)))
        book.create_project("p", "a")
        doc = path.parent / "plan.txt"
        doc.write_text("매출 목표 30억", encoding="utf-8")
        book.attach("p", doc, "사업계획 확정", "a")
        raw = path.read_bytes()
        self.assertNotIn("사업계획".encode(), raw)
        self.assertNotIn("매출".encode(), raw)
        got = book.fetch("p", name="plan", into=path.parent / "out")
        self.assertEqual(Path(got["path"]).read_text(encoding="utf-8"), "매출 목표 30억")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_then_open_through_the_assistant_and_personal_memory_stays_out(self):
        from graph_mind_mcp_server import build_server
        if True:          # mkdtemp: the server's background indexing may still hold the store
            root = Path(tempfile.mkdtemp())
            deck = root / "Graph-MIND_제안서.pptx"
            with zipfile.ZipFile(deck, "w") as z:
                z.writestr("ppt/slides/slide1.xml", "<a:t>제안</a:t>")
            db = root / "brain.sqlite"
            with mock.patch("project_ledger.open_with_default_app") as opened:
                async with Client(build_server(db)) as client:
                    await client.call_tool("brain_remember", {
                        "content": "개인 메모: 연봉 협상 전략", "title": "private",
                        "memory_type": "fact", "source_ref": "t"})
                    call = lambda **a: client.call_tool("brain_checkpoint", a)  # noqa: E731
                    refused = (await call(action="publish", project="graph-mind", type="progress",
                                          summary="x")).structured_content
                    self.assertEqual(refused["status"], "REFUSED")
                    await call(action="create_project", project="graph-mind")
                    up = (await call(action="attach", project="graph-mind", path=str(deck),
                                     summary="제안서 v1 완료")).structured_content
                    self.assertEqual(up["artifacts"][0]["version"], 1)
                    shown = (await call(action="open", project="graph-mind",
                                        name="제안서")).structured_content
                    self.assertTrue(shown["opened"])
                    opened.assert_called_once()
                    timeline = (await call(action="timeline",
                                           project="graph-mind")).structured_content
            ledger_file = Path(os.environ["GRAPH_MIND_HOME"]) / "ledger.sqlite"
            dump = "\n".join(sqlite3.connect(ledger_file).iterdump())
            self.assertNotIn("연봉", dump)                 # personal memory never reaches it
            self.assertEqual(len(timeline["checkpoints"]), 1)


@unittest.skipUnless(importlib.util.find_spec("pgserver"), "pgserver is built for Python <3.13")
class PostgresLedgerTests(unittest.TestCase):
    def test_the_same_ledger_on_a_postgres_hub(self):
        import pgserver
        import psycopg
        from cryptography.fernet import Fernet
        root = Path(tempfile.gettempdir()) / f"gm-ledger-{os.getpid()}"
        root.mkdir(exist_ok=True)
        server = pgserver.get_server(root / "pg", cleanup_mode="stop")
        try:
            with psycopg.connect(server.get_uri(), autocommit=True) as connection:
                book = Ledger(connection, postgres=True,
                              key=Fernet(base64.urlsafe_b64encode(b"p" * 32)))
                book.create_project("team", "a")
                doc = root / "spec.md"
                doc.write_text("# 스펙", encoding="utf-8")
                book.attach("team", doc, "스펙 v1", "a")
                got = book.fetch("team", name="spec", into=root / "out")
                self.assertEqual(Path(got["path"]).read_text(encoding="utf-8"), "# 스펙")
        finally:
            server.cleanup()


if __name__ == "__main__":
    unittest.main()
