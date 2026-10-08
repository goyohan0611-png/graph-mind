"""Conservative semantic promotion gate for captured conversation turns."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import re
import sqlite3

from local_brain import LocalBrainStore


GATE_VERSION = "semantic-memory-gate-v0.3.1"
PREFERENCE = re.compile(r"(?:선호|좋아(?:해|한다|함)|싫어(?:해|한다|함)|원해|원한다|했으면)")
DECISION = re.compile(r"(?:앞으로|하기로|결정(?:했|한다|함)|채택(?:했|한다|함)|고정(?:하자|한다|함)|방향.{0,20}(?:잡|가자|간다))")
REMEMBER = re.compile(r"(?:기억해|기억해줘|잊지 ?마|계속 기억)")
PROJECT = re.compile(r"(?:프로젝트|제품|서비스).{0,40}(?:목표|방향|원칙|목적)")
QUESTION_ONLY = re.compile(r"(?:뭐야|무엇|왜|어떻게|해줘|봐줘|고쳐줘|수정해줘|리뷰해줘)\s*[?？\\]*$")


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def classify_durable_turn(role, content):
    """Return a strict category only for explicit, durable user intent."""
    if role != "user":
        return None, "NON_USER_TURN"
    text = content.strip()
    if not text:
        return None, "EMPTY_TURN"
    if "[REDACTED" in text:
        return None, "REDACTED_CONTENT_REQUIRES_REVIEW"
    if len(text) > 2000 and not REMEMBER.search(text):
        return None, "LONG_CONTENT_REQUIRES_REVIEW"
    if REMEMBER.search(text):
        return "FACT", "EXPLICIT_REMEMBER_REQUEST"
    if QUESTION_ONLY.search(text) or text.endswith(("?", "？")):
        return None, "CURRENT_OR_GENERAL_QUESTION"
    if PROJECT.search(text):
        return "PROJECT_PRINCIPLE", "EXPLICIT_PROJECT_PRINCIPLE"
    if DECISION.search(text):
        return "DECISION", "EXPLICIT_DURABLE_DECISION"
    if PREFERENCE.search(text) and re.search(r"(?:나는|난|내가|내 |앞으로|항상)", text):
        return "PREFERENCE", "EXPLICIT_USER_PREFERENCE"
    return None, "NO_EXPLICIT_DURABLE_SIGNAL"


class SemanticMemoryIngestion:
    def __init__(self, path, *, now=None):
        self.path = Path(path)
        self.now = now or _now
        # Claude, Codex and the capture service each open this file from their own process.
        # WAL lets them read while one writes; the timeout makes a writer wait its turn
        # instead of failing with "database is locked".
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_ingestion_audit(
          turn_id TEXT PRIMARY KEY,
          gate_version TEXT NOT NULL,
          disposition TEXT NOT NULL,
          reason TEXT NOT NULL,
          memory_id TEXT,
          processed_at TEXT NOT NULL,
          source_fingerprint TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS semantic_ingestion_disposition
          ON semantic_ingestion_audit(disposition,processed_at);
        """)
        with self.db:
            self.db.execute("""UPDATE semantic_ingestion_audit
              SET disposition='EVIDENCE_ONLY'
              WHERE disposition='IGNORED'""")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

    def _unprocessed(self, limit):
        exists = self.db.execute("""SELECT 1 FROM sqlite_master
          WHERE type='table' AND name='conversation_turns'""").fetchone()
        if not exists:
            return []
        return self.db.execute("""SELECT t.* FROM conversation_turns t
          LEFT JOIN semantic_ingestion_audit a ON a.turn_id=t.turn_id
          WHERE a.turn_id IS NULL ORDER BY t.revision LIMIT ?""", (limit,)).fetchall()

    def process_new(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("INVALID_SEMANTIC_INGESTION_LIMIT")
        rows = self._unprocessed(limit)
        promoted, evidence_only = [], []
        for row in rows:
            category, reason = classify_durable_turn(row["role"], row["content_redacted"])
            fingerprint = _digest("\0".join((row["turn_id"], row["role"],
                                             row["content_redacted"])))
            memory_id = None
            disposition = "EVIDENCE_ONLY"
            if category:
                memory_id = "semantic-" + _digest(
                    row["turn_id"] + "\0" + category)[:32]
                memory = {"memory_id": memory_id, "scope": row["scope"],
                    "memory_type": category,
                    "memory_class": {
                        "PREFERENCE": "PREFERENCE", "DECISION": "DECISION",
                        "FACT": "FACT", "PROJECT_PRINCIPLE": "STATE",
                    }.get(category, "FACT"),
                    "modality": "CONVERSATION", "retention": "DURABLE",
                    "importance": 0.85 if reason == "EXPLICIT_REMEMBER_REQUEST" else 0.75,
                    "confidence": 1.0,
                    "attributes": {"gate_version": GATE_VERSION,
                                   "promotion_reason": reason},
                    "related_memory_ids": ["conversation-turn:" + row["turn_id"]],
                    "title": f"{category}: {row['content_redacted'][:80]}",
                    "content": row["content_redacted"],
                    "effective_at": row["happened_at"], "known_at": self.now(),
                    "actor": "user", "tags": [category, "automatic-semantic-memory"],
                    "entities": [], "provenance": {
                        "source_type": "captured-conversation-turn",
                        "source_ref": ("conversation-turn:" + row["turn_id"]
                                       + f"#chars=0-{len(row['content_redacted'])}")},
                    "supersedes_memory_id": None}
                with LocalBrainStore(self.path) as brain:
                    brain.remember(memory)
                disposition = "PROMOTED"
                promoted.append({"turn_id": row["turn_id"], "memory_id": memory_id,
                                 "memory_type": category, "reason": reason})
            else:
                evidence_only.append({"turn_id": row["turn_id"], "reason": reason})
            with self.db:
                self.db.execute("""INSERT INTO semantic_ingestion_audit(
                  turn_id,gate_version,disposition,reason,memory_id,processed_at,
                  source_fingerprint) VALUES(?,?,?,?,?,?,?)""", (
                    row["turn_id"], GATE_VERSION, disposition, reason, memory_id,
                    self.now(), fingerprint))
        return {"status": "SEMANTIC_TURNS_PROCESSED" if rows else "NO_NEW_TURNS",
                "processed_turns": len(rows), "promoted_count": len(promoted),
                "evidence_only_count": len(evidence_only), "promoted": promoted,
                "evidence_only_reasons": dict(sorted({item["reason"]:
                    sum(1 for other in evidence_only
                        if other["reason"] == item["reason"])
                    for item in evidence_only}.items()))}
