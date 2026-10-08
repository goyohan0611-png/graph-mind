from pathlib import Path
import tempfile
import unittest

from mcp import Client

from associative_memory import AssociativeMemoryIndex
from coding_memory import CodingMemoryStore
from conversation_memory import ConversationMemoryStore
from development_memory import DevelopmentMemoryStore
from graph_mind_mcp_server import build_server
from local_brain import LocalBrainStore


T1 = "2026-01-01T10:00:00"
T2 = "2026-01-01T11:00:00"


def seed_event(event_id, kind, subject, payload, when=T1, supersedes=None):
    return {"event_id": event_id, "project_id": "project:test",
            "session_id": "session:seed", "event_type": kind,
            "effective_at": when, "known_at": when, "actor": "user",
            "subject_id": subject, "payload": payload,
            "provenance": {"source_type": "fixture", "source_ref": "test"},
            "supersedes_event_id": supersedes}


class GraphMindMcpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_share_one_persistent_project_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "memory.sqlite"
            with DevelopmentMemoryStore(db) as store:
                store.record_events([
                    seed_event("objective", "OBJECTIVE_SET", "project",
                               {"objective": "Persistent cross-model memory"}),
                    seed_event("decision", "DECISION_RECORDED", "storage",
                               {"decision": "Use append-only events",
                                "reason": "Preserve audit history"}),
                ])
            with LocalBrainStore(db) as brain:
                brain.remember({"memory_id": "mind-overview", "scope": "global",
                    "memory_type": "PROJECT", "title": "Graph-MIND 프로젝트",
                    "content": "모델과 세션을 바꿔도 이어지는 사용자 소유 로컬 뇌",
                    "effective_at": T1, "known_at": T1, "actor": "user",
                    "tags": ["마인드", "MIND"], "entities": ["Graph-MIND"],
                    "provenance": {"source_type": "fixture", "source_ref": "test"},
                    "supersedes_memory_id": None})
            with CodingMemoryStore(db) as coding:
                coding.record({"change_id": "code-change-1", "project_id": "project:test",
                    "session_id": "session:seed", "workspace": str(Path(directory)),
                    "happened_at": T1, "known_at": T1, "change_kind": "MODIFIED",
                    "file_path": "memory.py", "language": "py",
                    "before_sha256": "a", "after_sha256": "b",
                    "reason": "Remember code work", "related_tests": ["memory-test"],
                    "symbols": [{"name": "recall", "kind": "FUNCTION", "side": "AFTER",
                                 "start_line": 1, "end_line": 2}],
                    "diff_ref": str(Path(directory) / "change.patch"),
                    "before_ref": None, "after_ref": None,
                    "provenance": {"source_type": "fixture", "source_ref": "memory.py"}})
            with ConversationMemoryStore(db) as conversations:
                conversations.record({"turn_id": "turn-1", "client": "codex",
                    "client_session_id": "session:seed", "scope": "project:test",
                    "cwd": str(Path(directory)), "happened_at": T1, "known_at": T1,
                    "role": "user", "content_redacted": "어제 결제 함수를 고쳤어",
                    "raw_sha256": "abc", "redaction_count": 0,
                    "source_path": "fixture.jsonl", "source_ordinal": 1})
            with AssociativeMemoryIndex(db) as associative:
                associative.sync_sources()
            server = build_server(db)
            async with Client(server) as client:
                listed = await client.list_tools()
                by_name = {tool.name: tool for tool in listed.tools}
                names = set(by_name)
                self.assertEqual(names, {"brain_remember", "brain_recall", "brain_context",
                                         "brain_associate", "brain_index", "brain_timeline", "brain_folder",
                                         "conversation_recall", "code_activity",
                                         "memory_record", "project_status",
                                         "resume_project", "explain_decision"})
                self.assertFalse(by_name["brain_remember"].annotations.read_only_hint)
                self.assertTrue(by_name["brain_recall"].annotations.read_only_hint)
                self.assertTrue(by_name["brain_context"].annotations.read_only_hint)
                self.assertTrue(by_name["brain_associate"].annotations.read_only_hint)
                self.assertTrue(by_name["conversation_recall"].annotations.read_only_hint)
                self.assertTrue(by_name["code_activity"].annotations.read_only_hint)
                self.assertFalse(by_name["memory_record"].annotations.read_only_hint)
                for name in ("project_status", "resume_project", "explain_decision"):
                    self.assertTrue(by_name[name].annotations.read_only_hint)
                    self.assertTrue(by_name[name].annotations.idempotent_hint)
                recalled = await client.call_tool("brain_recall", {
                    "query": "마인드 프로젝트가 뭐야?"})
                self.assertFalse(recalled.is_error)
                self.assertEqual(recalled.structured_content["status"], "KNOWN")
                self.assertEqual(recalled.structured_content["matches"][0]["memory_id"],
                                 "mind-overview")
                conversation = await client.call_tool("conversation_recall", {
                    "query": "결제 함수", "scopes": ["project:test"]})
                self.assertEqual(conversation.structured_content["status"], "KNOWN")
                federated = await client.call_tool("brain_recall", {
                    "query": "결제 함수", "scopes": ["project:test"]})
                self.assertEqual(federated.structured_content["status"], "KNOWN")
                self.assertEqual(len(federated.structured_content["conversation_turns"]), 1)
                context = await client.call_tool("brain_context", {
                    "query": "previous memory project", "policy": "always",
                    "max_chars": 1000})
                self.assertFalse(context.is_error)
                self.assertEqual(context.structured_content["status"], "KNOWN")
                self.assertLessEqual(context.structured_content["used_chars"], 1000)
                skipped = await client.call_tool("brain_context", {
                    "query": "Explain photosynthesis", "policy": "auto"})
                self.assertEqual(skipped.structured_content["status"], "SKIPPED")
                associated = await client.call_tool("brain_associate", {
                    "query": "memory project", "hops": 2, "grounding": "literal"})
                self.assertEqual(associated.structured_content["status"], "KNOWN")
                self.assertEqual(associated.structured_content["evidence_status"],
                                 "CANDIDATES_REQUIRE_SOURCE_VERIFICATION")
                code = await client.call_tool("code_activity", {
                    "project_id": "project:test", "symbol": "recall",
                    "happened_from": "2026-01-01T00:00:00+09:00",
                    "happened_to": "2026-01-01T23:59:59+09:00",
                    "max_diff_chars": 100000})
                self.assertFalse(code.is_error)
                self.assertEqual(code.structured_content["status"], "KNOWN")
                self.assertEqual(code.structured_content["changes"][0]["file_path"],
                                 "memory.py")
                recorded = await client.call_tool("memory_record", {
                    "project_id": "project:test", "session_id": "session:mcp",
                    "event_type": "NEXT_ACTION_SET", "subject_id": "mcp-next",
                    "payload": {"action": "Connect another model"},
                    "source_ref": "mcp-test"})
                self.assertFalse(recorded.is_error)
                status = await client.call_tool("project_status", {
                    "project_id": "project:test", "as_of": "2999-01-01T00:00:00"})
                self.assertFalse(status.is_error)
                self.assertEqual(status.structured_content["next_actions"][0]["action"],
                                 "Connect another model")
                resumed = await client.call_tool("resume_project", {
                    "project_id": "project:test", "as_of": "2999-01-01T00:00:00"})
                self.assertEqual(resumed.structured_content["objective"]["value"],
                                 "Persistent cross-model memory")
                explained = await client.call_tool("explain_decision", {
                    "project_id": "project:test", "decision_id": "storage",
                    "as_of": "2999-01-01T00:00:00"})
                self.assertEqual(explained.structured_content["decision_reason"],
                                 "Preserve audit history")

            with DevelopmentMemoryStore(db) as reopened:
                result = reopened.resume_project("project:test", as_of="2999-01-01T00:00:00")
            self.assertEqual(result["next_actions"][0]["action"], "Connect another model")


if __name__ == "__main__":
    unittest.main()
