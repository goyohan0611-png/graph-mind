"""Project brains in a shared cloud folder: three people on three PCs (each its own config, key and
cache), one folder that the cloud drive syncs. No server, no network setup."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import json
import os
import tempfile
import unittest

import project_folder as pf
import project_ledger as pl


class FolderProjectTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cloud = self.root / "GoogleDrive"
        self.cloud.mkdir()
        self.saved = {k: os.environ.get(k) for k in ("GRAPH_MIND_CONFIG", "GRAPH_MIND_HOME")}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def pc(self, name):
        """Act as this person's PC from here on."""
        os.environ["GRAPH_MIND_CONFIG"] = str(self.root / f"{name}.json")
        os.environ["GRAPH_MIND_HOME"] = str(self.root / f"{name}-home")

    def book(self, folder, code):
        return pf.FolderLedger(folder, code)

    def test_a_review_round_trip_through_a_shared_folder(self):
        self.pc("yohan")
        made = pf.create("alpha", "yohan", where=self.cloud)
        folder = Path(made["folder"])
        code = json.loads(Path(os.environ["GRAPH_MIND_CONFIG"]).read_text("utf-8")
                          )["folder_projects"][0]["code"]
        owner = self.book(folder, code)
        self.assertEqual(owner._me(), ("yohan", "owner"))
        minji_code = owner.grant("alpha", "minji", "editor", "yohan")["invite_code"]
        jun_code = owner.grant("alpha", "jun", "viewer", "yohan")["invite_code"]
        deck = self.root / "plan.md"
        deck.write_text("# 계획\n- 1단계\n", encoding="utf-8")
        owner.attach("alpha", deck, "plan v1", "yohan")
        owner.db.close()
        # what the cloud sees: sealed lines only
        stored = "".join(p.read_text("utf-8") for p in (folder / "log").glob("*.jsonl"))
        self.assertNotIn("계획", stored)
        self.assertNotIn("plan v1", stored)

        self.pc("minji")
        joined = pf.join(minji_code, str(folder))
        self.assertEqual((joined["joined_as"], joined["role"]), ("minji", "editor"))
        minji = self.book(folder, pf.decode(minji_code))
        self.assertEqual(minji.timeline("alpha")[0]["summary"], "plan v1")
        opened = minji.fetch("alpha", name="plan", into=self.root / "minji-dl")
        self.assertEqual(Path(opened["path"]).read_text("utf-8"), deck.read_text("utf-8"))
        minji.db.close()

        self.pc("yohan")
        owner = self.book(folder, code)
        review = owner.request_review("alpha", "minji", "yohan", name="plan")["id"]
        owner.db.close()

        self.pc("minji")
        minji = self.book(folder, pf.decode(minji_code))
        self.assertEqual([e["key"] for e in pl.inbox_events(minji)], [f"{review}:requested"])
        minji.comment(review, "2단계가 빠졌어요", "minji", files=[("memo.txt", b"step 2?")])
        minji.respond_review(review, "changes_requested", "minji", note="2단계 추가해 주세요")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):     # not in her name
            minji.publish("alpha", "decision", "요한이 결정함", "yohan")
        minji.db.close()

        self.pc("jun")
        pf.join(jun_code, str(folder))
        jun = self.book(folder, pf.decode(jun_code))
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):     # a viewer
            jun.publish("alpha", "progress", "읽기만 가능", "jun")
        jun.comment(review, "저도 봤어요", "jun")                   # a viewer may comment
        jun.db.close()

        self.pc("yohan")
        owner = self.book(folder, code)
        events = [e["key"] for e in pl.inbox_events(owner)]
        self.assertIn(f"{review}:changes_requested", events)
        self.assertEqual([c["by"] for c in owner.comments(review)], ["minji", "jun"])
        self.assertEqual(owner.members("alpha"), [{"member": "yohan", "role": "owner"},
                                                  {"member": "minji", "role": "editor"},
                                                  {"member": "jun", "role": "viewer"}])
        owner.db.close()

    def test_forged_tampered_and_removed_lines_are_never_applied(self):
        self.pc("yohan")
        folder = Path(pf.create("alpha", "yohan", where=self.cloud)["folder"])
        code = json.loads(Path(os.environ["GRAPH_MIND_CONFIG"]).read_text("utf-8")
                          )["folder_projects"][0]["code"]
        owner = self.book(folder, code)
        minji_code = owner.grant("alpha", "minji", "editor", "yohan")["invite_code"]
        owner.db.close()

        self.pc("minji")
        pf.join(minji_code, str(folder))
        minji = self.book(folder, pf.decode(minji_code))
        forged = pl.Ledger._checkpoint_record(minji, "alpha", "decision", "요한 명의 결정", "yohan",
                                              [], [], None, "", None)
        minji._append([{"kind": "sql", "t": "publish", "p": [
            forged["id"], "alpha", pl._stamp(), forged["created_at"], "published",
            json.dumps(forged, ensure_ascii=False)]}])          # past her own PC's check
        minji.publish("alpha", "progress", "민지 진행", "minji")
        minji.db.close()

        self.pc("yohan")
        owner = self.book(folder, code)
        summaries = [c["summary"] for c in owner.timeline("alpha")]
        self.assertIn("민지 진행", summaries)
        self.assertNotIn("요한 명의 결정", summaries)               # forged in his name
        reasons = [r for (r,) in owner.db.execute("SELECT reason FROM folder_rejected")]
        self.assertIn("published in someone else's name", reasons)
        owner.revoke("alpha", "minji", "yohan")
        owner.db.close()

        self.pc("minji")
        minji = self.book(folder, pf.decode(minji_code))
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            minji.publish("alpha", "progress", "나간 뒤", "minji")
        minji.db.close()

        # a line altered in the cloud: its signature no longer holds
        minji_log = [p for p in (folder / "log").glob("*.jsonl")
                     if p.name != f"{pf._device_id(code['o'])}.jsonl"][0]
        lines = minji_log.read_text("utf-8").splitlines()
        last = json.loads(lines[-1])
        last["seq"] += 100
        lines.append(json.dumps(last))
        minji_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.pc("yohan")
        owner = self.book(folder, code)
        reasons = [r for (r,) in owner.db.execute("SELECT reason FROM folder_rejected")]
        self.assertIn("bad signature", reasons)
        owner.db.close()


class FolderToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_people_through_their_assistants_with_no_hub(self):
        """No hub anywhere: '프로젝트 만들어줘' lands in the cloud drive's folder; '민지 초대해'
        gives a code and the folder to share; her assistant joins with the code and opens it."""
        from mcp import Client
        from graph_mind_mcp_server import build_server
        root = Path(tempfile.mkdtemp())
        cloud = root / "OneDrive"
        cloud.mkdir()
        saved = {k: os.environ.get(k) for k in ("GRAPH_MIND_CONFIG", "GRAPH_MIND_HOME",
                                                "GRAPH_MIND_CLOUD_ROOTS")}
        try:
            os.environ.update(GRAPH_MIND_CONFIG=str(root / "yohan.json"),
                              GRAPH_MIND_HOME=str(root / "yohan-home"),
                              GRAPH_MIND_CLOUD_ROOTS=str(cloud))
            async with Client(build_server(root / "a.sqlite")) as a:
                call = lambda **k: a.call_tool("brain_checkpoint", k)  # noqa: E731
                await call(action="set_name", name="요한")
                made = (await call(action="create_project", project="제안서")).structured_content
                self.assertTrue(made["folder"].startswith(str(cloud)))
                note = root / "안.md"
                note.write_text("# 제안 v1", encoding="utf-8")
                await call(action="attach", project="제안서", path=str(note), summary="v1")
                invited = (await call(action="invite", member="민지", role="editor",
                                      projects=["제안서"])).structured_content
            code = invited["granted"][0]["invite_code"]
            self.assertIn(made["folder"], invited["granted"][0]["share"])

            os.environ.update(GRAPH_MIND_CONFIG=str(root / "minji.json"),
                              GRAPH_MIND_HOME=str(root / "minji-home"))
            async with Client(build_server(root / "b.sqlite")) as b:
                call = lambda **k: b.call_tool("brain_checkpoint", k)  # noqa: E731
                joined = (await call(action="join_team", code=code)).structured_content
                self.assertEqual((joined["joined_as"], joined["role"]), ("민지", "editor"))
                timeline = (await call(action="timeline", project="제안서")).structured_content
                self.assertEqual(timeline["checkpoints"][0]["summary"], "v1")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
