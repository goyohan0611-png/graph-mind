import os

from graph_mind import documents
from graph_mind.context_packet import _conversation_item
from graph_mind.conversation_memory import ConversationMemoryStore


def test_sections_follow_headings():
    text = "intro\n# Login\nemail\n## Kakao\nkakao too\n```\n# not a heading\n```\n# Pay\ncard"
    assert documents.sections(text) == [
        ("", "intro"), ("Login", "email"),
        ("Login > Kakao", "kakao too\n```\n# not a heading\n```"), ("Pay", "card")]


def test_edits_keep_the_old_text_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "WALK_SECONDS", 0)
    folder = tmp_path / "plan"
    folder.mkdir()
    spec = folder / "spec.md"
    spec.write_text("# Login\nUsers sign in with email.\n# Pay\nCard only.", encoding="utf-8")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "x.md").write_text("# Skip\nme", encoding="utf-8")
    times = iter(f"2026-10-{d:02d}T09:00:00" for d in range(1, 30))
    with ConversationMemoryStore(tmp_path / "m.sqlite") as store:
        capture = documents.DocumentCapture([folder], store, state_path=tmp_path / "s.json",
                                            now=lambda: next(times))
        assert capture.scan()["recorded_turns"] == 2
        assert capture.scan()["recorded_turns"] == 0              # unchanged: nothing new
        spec.write_text("# Login\nUsers sign in with Kakao.\n# Pay\nCard only.", encoding="utf-8")
        os.utime(spec, (1, 2_000_000_000))
        result = capture.scan()
        assert result["recorded_turns"] == 1 and result["replaced_sections"] == 1
        hits = store.search("sign in", limit=10)["turns"]
        assert {h["content"].splitlines()[-1] for h in hits} == {
            "Users sign in with Kakao.", "Users sign in with email."}
        old = documents.replaced_at(store.db, [h["turn_id"] for h in hits])
        assert len(old) == 1
        stale = next(h for h in hits if h["turn_id"] in old)
        assert "email" in stale["content"] and stale["content"].startswith("plan/spec.md > Login")
        item = _conversation_item({**stale, "replaced_at": old[stale["turn_id"]]})
        assert item["title"].startswith("document section, replaced on 2026-10-")
        spec.unlink()
        assert capture.scan()["replaced_sections"] == 2           # deleted: all of it replaced
