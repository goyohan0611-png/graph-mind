import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import json
import tempfile
import unittest

from conversation_memory import (ClaudeCodeConversationCapture, ClaudeCoworkConversationCapture,
                                 CodexConversationCapture,
                                 ConversationMemoryStore)


def row(ordinal, role, text, timestamp="2026-09-16T10:00:00"):
    return {"timestamp": timestamp, "ordinal": ordinal, "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text" if role == "user"
                                     else "output_text", "text": text}]}}


class ConversationMemoryTests(unittest.TestCase):
    def test_incremental_capture_redacts_secrets_and_searches_fts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions, workspace = root / "sessions", root / "project"
            sessions.mkdir(); workspace.mkdir()
            source = sessions / "rollout-test.jsonl"
            meta = {"type": "session_meta", "payload": {
                "session_id": "session:test", "cwd": str(workspace)}}
            source.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json",
                    workspace_scopes={str(workspace): "project:demo"},
                    now=lambda: "2026-09-16T10:01:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row(1, "user",
                        "어제 결제 모듈을 수정했어 sk-proj-abcdefghijklmnopqrstuvwxyz"),
                        ensure_ascii=False) + "\n")
                    handle.write(json.dumps(row(2, "assistant",
                        "payment.py의 charge 함수를 수정했습니다."),
                        ensure_ascii=False) + "\n")
                first = capture.scan()
                second = capture.scan()
                found = store.search("결제 모듈", scopes=["project:demo"])
            self.assertEqual(first["recorded_turns"], 2)
            self.assertEqual(first["redactions"], 1)
            self.assertEqual(second["status"], "NO_NEW_TURNS")
            self.assertEqual(found["status"], "KNOWN")
            self.assertIn("[REDACTED_OPENAI_KEY]", found["turns"][0]["content"])
            self.assertNotIn("sk-proj-", found["turns"][0]["content"])

    def test_utc_codex_timestamp_is_normalized_to_local_store_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); sessions = root / "sessions"; sessions.mkdir()
            source = sessions / "utc.jsonl"
            source.write_text(json.dumps({"type": "session_meta", "payload": {
                "session_id": "session:utc"}}) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json",
                    now=lambda: "2026-09-16T19:01:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row(1, "user", "UTC timestamp memory",
                        "2026-09-16T10:00:00Z")) + "\n")
                result = capture.scan()
                found = store.search("timestamp memory")
            self.assertEqual(result["recorded_turns"], 1)
            self.assertEqual(found["status"], "KNOWN")

    def test_existing_sessions_start_at_end_and_new_session_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); sessions = root / "sessions"; sessions.mkdir()
            old = sessions / "old.jsonl"
            old.write_text(json.dumps(row(1, "user", "old history")) + "\n",
                           encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json")
                capture.initialize_baseline()
                new = sessions / "new.jsonl"
                new.write_text(json.dumps(row(1, "user", "new session memory")) + "\n",
                               encoding="utf-8")
                result = capture.scan()
                old_result = store.search("old history")
                new_result = store.search("new session memory")
            self.assertEqual(result["recorded_turns"], 1)
            self.assertEqual(old_result["status"], "UNKNOWN")
            self.assertEqual(new_result["status"], "KNOWN")

    def test_claude_code_keeps_only_what_was_said(self):
        """Of everything a Claude Code transcript holds, only typed user text and assistant text
        are conversation. Tool calls, tool results, thinking, sub-agent side chains, meta records
        and injected harness notices must not be stored; secrets must be masked."""
        def line(kind, content, **extra):
            return {"type": kind, "sessionId": "cc:1", "cwd": extra.pop("cwd", None),
                    "timestamp": "2026-09-16T10:00:00", "uuid": extra.pop("uuid", kind),
                    "message": {"role": kind, "content": content}, **extra}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects, workspace = root / "projects", root / "project"
            (projects / "p").mkdir(parents=True); workspace.mkdir()
            source = projects / "p" / "cc.jsonl"
            source.write_text(json.dumps({"type": "summary", "sessionId": "cc:1",
                                          "cwd": str(workspace)}) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = ClaudeCodeConversationCapture(projects, store,
                    state_path=root / "cursor.json",
                    workspace_scopes={str(workspace): "project:demo"},
                    now=lambda: "2026-09-16T10:01:00")
                capture.initialize_baseline()
                rows = [
                    line("user", "콩이를 제주도에 데려갔어 sk-proj-abcdefghijklmnopqrstuvwxyz"
                                 "<system-reminder>harness notice</system-reminder>"),
                    line("assistant", [{"type": "thinking", "thinking": "private"},
                                       {"type": "text", "text": "협재 해변이었군요."},
                                       {"type": "tool_use", "name": "Bash", "input": {}}]),
                    line("assistant", [{"type": "tool_use", "name": "Read", "input": {}}]),
                    line("user", [{"type": "tool_result", "content": "file dump"}]),
                    line("user", "side chain chatter", isSidechain=True),
                    line("user", "meta record", isMeta=True),
                    {"type": "attachment", "sessionId": "cc:1"},
                    {"type": "attachment", "sessionId": "cc:1", "timestamp": "2026-09-16T10:00:05",
                     "uuid": "q1", "attachment": {"type": "queued_command", "humanTurn": True,
                                                  "prompt": "작업 중에 보낸 말: 김치볶음밥 먹었어",
                                                  "commandMode": "prompt"}},
                    {"type": "queue-operation", "sessionId": "cc:1",
                     "content": "작업 중에 보낸 말: 김치볶음밥 먹었어"},
                    {"type": "attachment", "sessionId": "cc:1", "uuid": "n1",
                     "attachment": {"type": "queued_command", "commandMode": "task-notification",
                                    "prompt": "background task finished"}},
                ]
                with source.open("a", encoding="utf-8") as handle:
                    for item in rows:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                result = capture.scan()
                found = store.search("콩이 제주도", scopes=["project:demo"])
                answer = store.search("협재 해변", scopes=["project:demo"])
                noise = store.search("file dump")
                queued = store.search("김치볶음밥")
                notice = store.search("background task")
                # re-reading the whole transcript from the start must not store anything twice
                state = json.loads((root / "cursor.json").read_text(encoding="utf-8"))
                state["offsets"] = {key: 0 for key in state["offsets"]}
                (root / "cursor.json").write_text(json.dumps(state), encoding="utf-8")
                again = capture.scan()
            self.assertEqual(result["recorded_turns"], 3)
            self.assertEqual(len(queued["turns"]), 1, "a mid-task message is stored once")
            self.assertEqual(notice["status"], "UNKNOWN", "task notifications are not speech")
            self.assertTrue(all(r["disposition"] == "ALREADY_RECORDED"
                                for r in again["record_results"]), again)
            self.assertEqual(result["redactions"], 1)
            said = found["turns"][0]["content"]
            self.assertIn("[REDACTED_OPENAI_KEY]", said)
            self.assertNotIn("harness notice", said)
            self.assertEqual(answer["status"], "KNOWN")
            self.assertNotIn("private", answer["turns"][0]["content"])
            self.assertEqual(noise["status"], "UNKNOWN")

    def test_cowork_reads_only_transcripts(self):
        """Cowork sessions sit next to credentials, audit logs and sub-agent chains
        and uploads. Only the session transcripts are conversation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "sessions" / "a" / "b" / "local_x"
            chat = session / ".claude" / "projects" / "p"
            (chat / "s1" / "subagents").mkdir(parents=True)
            (session / ".claude" / ".credentials.json").write_text('{"token": "secret"}')
            (session / "audit.jsonl").write_text('{"type": "user", "message": "audit"}\n')
            (chat / "s1" / "subagents" / "agent.jsonl").write_text("")
            transcript = chat / "s1.jsonl"
            transcript.write_text("")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = ClaudeCoworkConversationCapture(root / "sessions", store,
                    state_path=root / "cursor.json", now=lambda: "2026-10-07T10:01:00")
                capture.initialize_baseline()
                transcript.write_text(json.dumps({
                    "type": "user", "sessionId": "cw:1", "uuid": "u1",
                    "timestamp": "2026-10-07T10:00:00",
                    "message": {"role": "user", "content": "코워크에서 보고서 정리했어"}},
                    ensure_ascii=False) + "\n", encoding="utf-8")
                files = capture._files()
                result = capture.scan()
                found = store.search("보고서 정리")
            self.assertEqual([f.name for f in files], ["s1.jsonl"])
            self.assertEqual(result["recorded_turns"], 1)
            self.assertEqual(found["turns"][0]["client"], "claude-cowork")


if __name__ == "__main__":
    unittest.main()
