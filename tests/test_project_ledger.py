"""Project brains on one PC: checkpoints published on request, results stored as versioned copies
in a file store apart from the ledger database, secrets refused, nothing personal let in."""
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


def ledger(path=None, key=None, files=None):
    return Ledger(sqlite3.connect(path or ":memory:"), key=key,
                  files=files or Path(tempfile.mkdtemp()) / "artifacts")


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
                                details="brain_view.py; 테스트 3개 통과; 남은 일: GIF",
                                source_refs=["codex-turn-abc", "commit:4211ae2"])
        timeline = book.timeline("graph-mind")
        self.assertEqual([c["id"] for c in timeline], [progress["id"], second["id"], first["id"]])
        self.assertEqual(timeline[2]["status"], "superseded")
        self.assertEqual(timeline[0]["source_refs"], ["codex-turn-abc", "commit:4211ae2"])
        self.assertIn("남은 일", timeline[0]["details"])
        book.retract(progress["id"], "yohan")
        self.assertNotIn(progress["id"], [c["id"] for c in book.timeline("graph-mind")])

    def test_text_is_refused_where_ids_belong_and_secrets_anywhere(self):
        book = ledger()
        book.create_project("p", "a")
        with self.assertRaisesRegex(ValueError, "SOURCE_REFS_ARE_IDS"):
            book.publish("p", "progress", "ok", "a", source_refs=["x" * 300])
        with self.assertRaisesRegex(ValueError, "SECRET_IN_CHECKPOINT"):
            book.publish("p", "progress", "ok", "a", details=f"키는 {KEY}")


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.files = self.dir / "store"
        self.book = ledger(self.dir / "ledger.sqlite", files=self.files)
        self.book.create_project("proposal", "yohan")

    def stored(self):
        return sorted(p for p in self.files.rglob("*") if p.is_file())

    def test_a_published_version_is_a_copy_kept_as_it_was(self):
        deck = self.dir / "work" / "제안서.pptx"
        deck.parent.mkdir()
        with zipfile.ZipFile(deck, "w") as z:
            z.writestr("ppt/slides/slide1.xml", "<a:t>Graph-MIND 제안 v1</a:t>")
        first = deck.read_bytes()
        v1 = self.book.attach("proposal", deck, "제안서 초안 완료", "yohan")
        self.assertTrue(deck.exists())                         # the working copy stays put
        self.assertEqual(self.book.attach("proposal", deck, "again", "yohan")["status"],
                         "UNCHANGED")
        with zipfile.ZipFile(deck, "a") as z:                  # edited after publishing
            z.writestr("ppt/slides/slide2.xml", "<a:t>피드백 반영</a:t>")
        v2 = self.book.attach("proposal", deck, "피드백 반영", "minji")
        self.assertEqual((v1["artifacts"][0]["version"], v2["artifacts"][0]["version"]), (1, 2))
        # stored under stable ids, the original name kept as metadata and file name
        location = Path(v1["artifacts"][0]["location"])
        self.assertEqual(location.parts[0], project_ledger.project_id("proposal"))
        self.assertEqual((location.parts[2], location.name), ("v1", "제안서.pptx"))
        latest = self.book.fetch("proposal", name="제안서", into=self.dir / "dl")
        self.assertEqual((latest["version"], latest["by"]), (2, "minji"))
        self.assertEqual(Path(latest["path"]).read_bytes(), deck.read_bytes())
        old = self.book.fetch("proposal", name="제안서", version=1, into=self.dir / "dl")
        self.assertEqual(Path(old["path"]).read_bytes(), first)    # v1 as it was published
        self.assertEqual(len(self.stored()), 2)

    def test_a_demo_folder_travels_as_one_bundle_and_opens_at_index(self):
        demo = self.dir / "demo"
        (demo / "img").mkdir(parents=True)
        (demo / "index.html").write_text("<link href=style.css><script src=app.js></script>",
                                         encoding="utf-8")
        (demo / "style.css").write_text("body{}", encoding="utf-8")
        (demo / "app.js").write_text("console.log(1)", encoding="utf-8")
        (demo / "img" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
        (demo / "node_modules").mkdir()
        (demo / "node_modules" / "big.js").write_text("x", encoding="utf-8")
        self.book.attach("proposal", demo, "데모 v1", "yohan")
        got = self.book.fetch("proposal", name="demo", into=self.dir / "dl")
        opened = Path(got["path"])
        self.assertEqual(opened.name, "index.html")
        for sibling in ("style.css", "app.js", "img/logo.png"):
            self.assertTrue((opened.parent / sibling).exists(), sibling)
        self.assertFalse((opened.parent / "node_modules").exists())

    def test_a_key_inside_a_file_or_an_office_document_stops_the_upload(self):
        code = self.dir / "settings.py"
        code.write_text(f"OPENAI_API_KEY = '{KEY}'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SECRET_IN_FILE"):
            self.book.attach("proposal", code, "settings", "yohan")
        deck = self.dir / "deck.pptx"
        with zipfile.ZipFile(deck, "w") as z:
            z.writestr("ppt/slides/slide1.xml", "<a:t>password: hunter2-very-long</a:t>")
        with self.assertRaisesRegex(ValueError, "SECRET_IN_FILE"):
            self.book.attach("proposal", deck, "deck", "yohan")
        self.assertEqual((self.book.timeline("proposal"), self.stored()), ([], []))

    def test_retract_removes_the_file_from_the_store(self):
        notes = self.dir / "notes.txt"
        notes.write_text("회의록", encoding="utf-8")
        checkpoint = self.book.attach("proposal", notes, "회의록", "yohan")
        self.book.retract(checkpoint["id"], "yohan")
        self.assertEqual(self.stored(), [])
        with self.assertRaisesRegex(ValueError, "ARTIFACT_NOT_FOUND"):
            self.book.fetch("proposal", name="notes", into=self.dir / "dl")

    def test_a_publish_that_fails_halfway_shows_no_file(self):
        notes = self.dir / "notes.txt"
        notes.write_text("회의록", encoding="utf-8")
        with mock.patch.object(self.book, "_insert_checkpoint",
                               side_effect=ConnectionError("network gone")), \
                self.assertRaises(ConnectionError):
            self.book.attach("proposal", notes, "회의록", "yohan")
        self.assertEqual(self.book.timeline("proposal"), [])
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM ledger_artifacts")
                         .fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "ARTIFACT_NOT_FOUND"):
            self.book.fetch("proposal", name="notes", into=self.dir / "dl")

    def test_a_copy_cut_off_while_writing_leaves_no_version_folder(self):
        store = project_ledger.FolderStore(self.dir / "cut")

        def pieces():
            yield b"first"
            raise ConnectionError("disk gone")
        with self.assertRaises(ConnectionError):
            store.put("proposal", "af-000000000000", 1, "x.bin", pieces())
        self.assertEqual(list((self.dir / "cut").rglob("v1*")), [])

    def test_a_git_folder_is_linked_with_the_stage_it_reached(self):
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
        self.assertEqual((link["kind"], len(link["commit"]), link["stage"]),
                         ("git", 40, "committed"))              # not pushed: never "done"
        self.assertEqual(self.stored(), [])


class SealedTests(unittest.TestCase):
    def test_with_a_key_summaries_are_not_stored_in_clear(self):
        from cryptography.fernet import Fernet
        path = Path(tempfile.mkdtemp()) / "hub.sqlite"
        book = ledger(path, key=Fernet(base64.urlsafe_b64encode(b"k" * 32)))
        book.create_project("p", "a")
        book.publish("p", "decision", "사업계획 확정", "a", details="매출 목표 30억")
        book.db.close()
        raw = path.read_bytes()
        self.assertNotIn("사업계획".encode(), raw)
        self.assertNotIn("매출".encode(), raw)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_then_open_through_the_assistant_and_personal_memory_stays_out(self):
        from graph_mind_mcp_server import build_server
        root = Path(tempfile.mkdtemp())
        deck = root / "Graph-MIND_제안서.pptx"
        with zipfile.ZipFile(deck, "w") as z:
            z.writestr("ppt/slides/slide1.xml", "<a:t>제안</a:t>")
        with mock.patch("project_ledger.open_with_default_app") as opened:
            async with Client(build_server(root / "brain.sqlite")) as client:
                await client.call_tool("brain_remember", {
                    "content": "개인 메모: 연봉 협상 전략", "title": "private",
                    "memory_type": "fact", "source_ref": "t"})
                call = lambda **a: client.call_tool("brain_checkpoint", a)  # noqa: E731
                refused = (await call(action="publish", project="graph-mind", type="progress",
                                      summary="x")).structured_content
                self.assertEqual(refused["status"], "REFUSED")
                await call(action="create_project", project="graph-mind", path=str(root))
                # no project named: the bound working folder picks it
                up = (await call(action="attach", workspace=str(root), path=str(deck),
                                 summary="제안서 v1 완료")).structured_content
                self.assertEqual(up["artifacts"][0]["version"], 1)
                shown = (await call(action="open", project="graph-mind",
                                    name="제안서")).structured_content
                self.assertTrue(shown["opened"])
                opened.assert_called_once()
                timeline = (await call(action="timeline",
                                       project="graph-mind")).structured_content
        home = Path(os.environ["GRAPH_MIND_HOME"])
        dump = "\n".join(sqlite3.connect(home / "ledger.sqlite").iterdump())
        self.assertNotIn("연봉", dump)                 # personal memory never reaches it
        for stored in (home / "hub" / "artifacts").rglob("*"):
            if stored.is_file():
                self.assertNotIn("연봉".encode(), stored.read_bytes())
        self.assertEqual(len(timeline["checkpoints"]), 1)


if __name__ == "__main__":
    unittest.main()
