"""Reviews in a project brain: a result travels with its context, a reviewer's AI opens the
exact version with what changed and why, a person's decision is recorded on that version only,
and each side hears about it once."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import os
import sqlite3
import tempfile
import unittest
import zipfile

import project_ledger as pl
from project_ledger import Ledger


def deck(path: Path, slides: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n, text in slides.items():
            z.writestr(f"ppt/slides/slide{n}.xml", f"<p:sld><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>")
    return path


class ReviewFlowTests(unittest.TestCase):
    """One ledger, two people taking turns (on a team hub each acts from their own PC)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.book = Ledger(sqlite3.connect(self.dir / "l.sqlite"), files=self.dir / "store")
        self.book.create_project("graph-mind", "yohan")

    def act_as(self, name):
        self.book.by = name
        return name

    def test_request_review_change_v2_approve_v2_only(self):
        a = self.act_as("yohan")
        v1 = self.book.attach("graph-mind", deck(self.dir / "제안서.pptx", {
            5: "가격 1,000만원", 8: "도입 일정 3월"}), "제안서 v1", a, context={
            "what": "가격표와 도입 일정 초안", "where": "5p 가격표, 8p 일정",
            "review_focus": "가격과 일정의 일관성"})
        request = self.book.request_review("graph-mind", "minji", a, name="제안서")
        again = self.book.request_review("graph-mind", "minji", a, name="제안서")
        self.assertTrue(again["duplicate"])                       # no second notification
        self.assertEqual([e["key"] for e in pl.inbox_events(self.book)], [])   # yohan: nothing yet

        b = self.act_as("minji")
        inbox = pl.inbox_events(self.book)
        self.assertEqual([e["key"] for e in inbox], [f"{request['id']}:requested"])
        opened = self.book.open_review(request["id"], into=self.dir / "dl", by=b)
        self.assertEqual(opened["review"]["status"], "in_review")  # opening is not approving
        self.assertEqual(opened["file"]["version"], 1)
        self.assertEqual(opened["context"]["publishers_account"]["review_focus"],
                         "가격과 일정의 일관성")
        self.assertEqual(opened["context"]["publishers_account"]["verification"], "not recorded")
        self.book.respond_review(request["id"], "changes_requested", b,
                                 note="8p 일정이 부록과 다릅니다")

        a = self.act_as("yohan")
        self.assertEqual([e["key"] for e in pl.inbox_events(self.book)],
                         [f"{request['id']}:changes_requested"])
        v2 = self.book.attach("graph-mind", deck(self.dir / "제안서.pptx", {
            5: "가격 1,000만원", 8: "도입 일정 4월"}), "제안서 v2: 일정 수정", a, context={
            "what": "도입 일정을 4월로", "where": "8p", "addresses": [request["id"]],
            "verification": "부록 일정과 대조함", "review_focus": "8p 일정"})
        second = self.book.request_review("graph-mind", "minji", a, name="제안서", version=2)

        b = self.act_as("minji")
        opened = self.book.open_review(second["id"], into=self.dir / "dl", by=b)
        self.assertEqual(opened["changes"]["from"], 1)
        self.assertIn("+[slide 8] 도입 일정 4월", opened["changes"]["changes"]["diff"])
        self.assertIn("-[slide 8] 도입 일정 3월", opened["changes"]["changes"]["diff"])
        self.assertEqual(opened["history"]["addresses"][0]["id"], request["id"])
        with self.assertRaisesRegex(ValueError, "WRONG_VERSION"):
            self.book.respond_review(second["id"], "approved", b, version=1)
        approved = self.book.respond_review(second["id"], "approved", b, version=2, note="확인")
        self.assertIn("only", approved["applies_to"])
        with self.assertRaisesRegex(ValueError, "ALREADY_DECIDED"):
            self.book.respond_review(second["id"], "approved", b)

        a = self.act_as("yohan")
        self.assertIn(f"{second['id']}:approved", [e["key"] for e in pl.inbox_events(self.book)])
        statuses = {r["target"].split("@")[1]: r["status"] for r in self.book.reviews("graph-mind")}
        self.assertEqual(statuses, {"v1": "changes_requested", "v2": "approved"})
        self.assertEqual((v1["artifacts"][0]["version"], v2["artifacts"][0]["version"]), (1, 2))

    def test_only_the_named_reviewer_decides(self):
        self.book.attach("graph-mind", deck(self.dir / "d.pptx", {1: "x"}), "d", "yohan")
        request = self.book.request_review("graph-mind", "minji", "yohan", name="d")
        self.act_as("jun")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            self.book.respond_review(request["id"], "approved", "jun")

    def test_notifications_are_delivered_once(self):
        self.book.attach("graph-mind", deck(self.dir / "d.pptx", {1: "x"}), "d", "yohan")
        self.book.request_review("graph-mind", "minji", "yohan", name="d")
        self.book.db.close()
        home = Path(os.environ["GRAPH_MIND_HOME"])
        with mock.patch.object(pl, "own_ledger", lambda: Ledger(
                sqlite3.connect(self.dir / "l.sqlite"), files=self.dir / "store", by="minji")), \
                mock.patch.object(pl, "toast") as shown:
            first = pl.notify_new()
            second = pl.notify_new()
        self.assertEqual((len(first), len(second)), (1, 0))
        shown.assert_called_once()
        self.assertTrue((home / "notified.json").exists())

    def test_office_files_compare_slide_by_slide(self):
        old = deck(self.dir / "a.pptx", {1: "제목", 2: "가격 100"}).read_bytes()
        new = deck(self.dir / "b.pptx", {1: "제목", 2: "가격 120"}).read_bytes()
        changes = pl.describe_changes("x.pptx", old, new)
        self.assertEqual((changes["lines_added"], changes["lines_removed"]), (1, 1))
        self.assertTrue(pl.describe_changes("x.bin", b"\0a", b"\0b")["binary"])


class NotificationTests(unittest.TestCase):
    def test_a_notification_that_failed_is_tried_again(self):
        """Only a notification that was shown counts as delivered."""
        import brain_log
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"GRAPH_MIND_CONFIG": str(root / "config.json"),
                                          "GRAPH_MIND_HOME": str(root / "home")}):
            brain_log._save({"user_name": "me"})
            book = pl.own_ledger()
            book.create_project("p", "me")
            (root / "a.txt").write_text("v1", encoding="utf-8")
            book.attach("p", root / "a.txt", "a v1", "me")
            asked = book.request_review("p", "me", "me", name="a.txt")
            book.db.close()
            with mock.patch.object(pl, "toast", return_value=False):
                self.assertEqual(pl.notify_new(), [])                  # failed: not recorded
            with mock.patch.object(pl, "toast", return_value=True):
                self.assertEqual([e["key"] for e in pl.notify_new()],
                                 [f"{asked['id']}:requested"])          # tried again, shown
                self.assertEqual(pl.notify_new(), [])                  # and only once


if __name__ == "__main__":
    unittest.main()
