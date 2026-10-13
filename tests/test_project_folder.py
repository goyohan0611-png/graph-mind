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

    def test_a_handover_through_a_shared_folder(self):
        self.pc("yohan")
        made = pf.create("alpha", "yohan", where=self.cloud)
        folder = Path(made["folder"])
        code = json.loads(Path(os.environ["GRAPH_MIND_CONFIG"]).read_text("utf-8")
                          )["folder_projects"][0]["code"]
        owner = self.book(folder, code)
        self.assertEqual(owner._me(), ("yohan", "owner"))
        minji_code = owner.grant("alpha", "minji", "editor", "yohan")["invite_code"]
        jun_code = owner.grant("alpha", "jun", "viewer", "yohan")["invite_code"]
        owner.set_instructions("alpha", "테스트 결과를 꼭 적을 것", "yohan")
        decision = owner.publish("alpha", "decision", "허브 대신 공유 폴더", "yohan",
                                 details="밖에서도 똑같이 되게. 남은 일: 설치 파일",
                                 context={"evidence": ["https://lab241.slack.com/p/1"]})
        owner.db.close()
        # what the cloud sees: sealed lines only
        stored = "".join(p.read_text("utf-8") for p in (folder / "log").glob("*.jsonl"))
        self.assertNotIn("공유 폴더", stored)
        self.assertNotIn("slack", stored)

        self.pc("minji")
        joined = pf.join(minji_code, str(folder))
        self.assertEqual((joined["joined_as"], joined["role"]), ("minji", "editor"))
        minji = self.book(folder, pf.decode(minji_code))
        picked = minji.timeline("alpha")[0]
        self.assertEqual((picked["summary"], picked["actor"]), ("허브 대신 공유 폴더", "yohan"))
        self.assertEqual(minji.instructions("alpha"), "테스트 결과를 꼭 적을 것")
        minji.publish("alpha", "progress", "설치 파일 초안", "minji", supersedes=None)
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):     # not in her name
            minji.publish("alpha", "decision", "요한이 결정함", "yohan")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):     # not hers to take back
            minji.retract(decision["id"], "minji")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):     # the owner's
            minji.set_instructions("alpha", "바꿔치기", "minji")
        minji.db.close()

        self.pc("jun")
        pf.join(jun_code, str(folder))
        jun = self.book(folder, pf.decode(jun_code))
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):     # a viewer
            jun.publish("alpha", "progress", "읽기만 가능", "jun")
        self.assertEqual([c["summary"] for c in jun.timeline("alpha")],
                         ["설치 파일 초안", "허브 대신 공유 폴더"])
        jun.db.close()

        self.pc("yohan")
        owner = self.book(folder, code)
        self.assertEqual(owner.timeline("alpha")[0]["actor"], "minji")
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
        gives a code and the folder to share; her assistant joins with the code and reads it."""
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
                await call(action="publish", project="제안서", type="progress", summary="v1",
                           details="슬랙에 v1 올림")
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
