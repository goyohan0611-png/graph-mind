"""The inbox page: it answers only this PC with this install's token, lists what waits for the
person, opens a review with its changes, opens the exact file and records the decision."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import json
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.request

import brain_log
import inbox_app
import project_ledger as pl


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class InboxAppTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"GRAPH_MIND_CONFIG": str(self.root / "config.json"),
                                                "GRAPH_MIND_HOME": str(self.root / "home")})
        self.env.start()
        self.port = free_port()
        brain_log._save({"user_name": "me", "inbox_port": self.port})
        book = pl.own_ledger()
        book.create_project("p", "me")
        plan = self.root / "plan.md"
        plan.write_text("# 계획\n- 1단계\n", encoding="utf-8")
        book.attach("p", plan, "plan v1", "me")
        plan.write_text("# 계획\n- 1단계\n- 2단계\n", encoding="utf-8")
        book.attach("p", plan, "plan v2", "me", context={"what": "2단계 추가",
                                                         "review_focus": "순서"})
        self.review = book.request_review("p", "me", "me", name="plan.md", version=2)["id"]
        book.db.close()
        self.server = inbox_app.serve()
        self.token = inbox_app.token()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()

    def call(self, path, body=None, token=True, host=None):
        headers = {"X-Graph-Mind-Token": self.token} if token else {}
        if host:
            headers["Host"] = host
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def test_only_this_pc_with_the_token_gets_in(self):
        self.assertEqual(self.call("/")[0], 200)
        self.assertIn("Graph-MIND 요청함", self.call("/")[1])
        self.assertEqual(self.call("/", host="evil.example:80")[0], 403)          # rebinding
        self.assertEqual(self.call("/api/inbox", token=False)[0], 403)
        self.assertEqual(self.call("/api/respond", {"review_id": self.review,
                                                    "decision": "approved"}, token=False)[0], 403)

    def test_a_review_from_the_list_to_the_decision(self):
        status, body = self.call("/api/inbox")
        listed = json.loads(body)
        self.assertEqual([r["id"] for r in listed["received"]], [self.review])
        self.assertEqual(listed["received"][0]["version"], 2)
        self.assertTrue(listed["received"][0]["unread"])
        self.assertEqual(listed["projects"], [{"name": "p", "role": "owner"}])

        opened = json.loads(self.call(f"/api/review/{self.review}")[1])
        self.assertEqual(opened["review"]["status"], "in_review")
        self.assertIn("+- 2단계", opened["changes"]["changes"]["diff"])
        self.assertEqual(opened["context"]["publishers_account"]["what"], "2단계 추가")
        self.assertTrue(opened["mine_to_answer"])

        with mock.patch.object(pl, "open_with_default_app") as launched:
            status, body = self.call("/api/open", {"review_id": self.review})
        self.assertEqual((status, json.loads(body)["version"]), (200, 2))
        self.assertTrue(Path(launched.call_args[0][0]).read_text(encoding="utf-8").endswith("2단계\n"))

        status, body = self.call("/api/respond", {"review_id": self.review, "decision": "approved",
                                                  "version": 1})
        self.assertEqual(status, 400)
        self.assertIn("WRONG_VERSION", body)
        status, body = self.call("/api/respond", {"review_id": self.review, "decision": "approved",
                                                  "note": "좋아요", "version": 2})
        self.assertEqual((status, json.loads(body)["status"]), (200, "approved"))

        # a decision can be taken back: the review is open again, the approval stays in history
        status, body = self.call("/api/respond", {"review_id": self.review, "decision": "withdrawn",
                                                  "note": "다시 볼게요"})
        self.assertEqual((status, json.loads(body)["status"]), (200, "in_review"))
        again = json.loads(self.call(f"/api/review/{self.review}")[1])["review"]
        self.assertEqual([a["decision"] for a in again["earlier_responses"]], ["approved"])
        self.assertEqual(again["response"]["decision"], "withdrawn")

    def test_deleting_hides_it_here_only_and_the_trash_restores_it(self):
        self.assertEqual(self.call("/api/mark", {"review_id": self.review, "hidden": True})[0], 200)
        self.assertTrue(json.loads(self.call("/api/inbox")[1])["received"][0]["hidden"])
        book = pl.own_ledger()                               # the shared record is untouched
        self.assertEqual([r["id"] for r in book.reviews("p")], [self.review])
        book.db.close()
        self.call("/api/mark", {"review_id": self.review, "hidden": False})
        self.assertFalse(json.loads(self.call("/api/inbox")[1])["received"][0]["hidden"])

    def test_a_comment_with_a_file_its_reply_and_an_edit(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/upload?name=shot.png",
                                         data=b"PNG-image-bytes", method="POST",
                                         headers={"X-Graph-Mind-Token": self.token})
        with urllib.request.urlopen(request, timeout=30) as response:
            sent = json.loads(response.read())
        status, body = self.call("/api/comment", {"review_id": self.review, "text": "여기 잘려요",
                                                  "uploads": [sent["upload"]]})
        said = json.loads(body)
        self.assertEqual((status, said["attachments"][0]["name"]), (200, "shot.png"))
        reply = json.loads(self.call("/api/comment", {"review_id": self.review, "text": "고쳤어요",
                                                      "parent_id": said["id"]})[1])
        self.assertEqual(reply["parent_id"], said["id"])
        self.call("/api/edit", {"review_id": self.review, "comment_id": said["id"],
                                "text": "여기 제목이 잘려요"})
        thread = json.loads(self.call(f"/api/review/{self.review}")[1])["comments"]
        self.assertEqual(thread[0]["text"], "여기 제목이 잘려요")
        self.assertEqual(thread[0]["edits"][0]["text"], "여기 잘려요")
        status, body = self.call(f"/api/attachment/{self.review}/{said['attachments'][0]['artifact_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(self.call("/api/comment", {"review_id": self.review, "text": "x",
                                                    "uploads": ["../../etc"]})[0], 400)

    def test_space_for_owners_and_the_operator_tab_only_where_the_hub_runs(self):
        used = json.loads(self.call("/api/storage/p")[1])
        self.assertEqual([f["name"] for f in used["files"]], ["plan.md"])
        self.assertEqual(self.call("/api/hub/usage")[0], 400)       # this PC runs no hub
        self.assertFalse(json.loads(self.call("/api/inbox")[1])["hosting"])

    def test_a_notification_links_to_its_review(self):
        self.assertTrue(inbox_app.url(self.review).endswith(f"#review/{self.review}"))
        self.assertIn(f":{self.port}/?t={self.token}", inbox_app.url(self.review))


if __name__ == "__main__":
    unittest.main()
