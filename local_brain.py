"""User-owned local memory shared across models, sessions and applications."""
from __future__ import annotations

from development_paths import use_wal

from pathlib import Path
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid

from temporal_state import timestamp
from universal_personal_memory import (PROFILE_FIELDS, SCHEMA_VERSION,
                                       default_profile, normalize_profile)


CONTRACT_VERSION = "local-brain-v0.1"


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _text(value, error):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value.strip()


def _string_list(value, error):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip()
                                              for item in value):
        raise ValueError(error)
    return list(dict.fromkeys(item.strip() for item in value))


def _normalized(value):
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def _tokens(value):
    return [item for item in _normalized(value).split() if len(item) > 1]


class LocalBrainStore:
    """Append-only general memory with deterministic, scope-aware recall."""

    def __init__(self, path, *, clock="SOURCE_LOCAL"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        # Claude, Codex and the capture service each open this file from their own process.
        # WAL lets them read while one writes; the timeout makes a writer wait its turn
        # instead of failing with "database is locked".
        self.db = sqlite3.connect(str(self.path), timeout=30)
        use_wal(self.db)
        self.db.row_factory = sqlite3.Row
        self.closed = False
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS local_brain_meta(
          key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS local_brain_memories(
          revision INTEGER PRIMARY KEY AUTOINCREMENT,
          memory_id TEXT NOT NULL UNIQUE,
          scope TEXT NOT NULL,
          memory_type TEXT NOT NULL,
          title TEXT NOT NULL,
          content TEXT NOT NULL,
          effective_at TEXT NOT NULL,
          known_at TEXT NOT NULL,
          actor TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          entities_json TEXT NOT NULL,
          provenance_json TEXT NOT NULL,
          supersedes_memory_id TEXT,
          fingerprint TEXT NOT NULL,
          FOREIGN KEY(supersedes_memory_id) REFERENCES local_brain_memories(memory_id));
        CREATE INDEX IF NOT EXISTS local_brain_scope_clock
          ON local_brain_memories(scope,known_at,effective_at,revision);
        CREATE INDEX IF NOT EXISTS local_brain_type
          ON local_brain_memories(memory_type,revision);
        CREATE TABLE IF NOT EXISTS local_brain_memory_profiles(
          memory_id TEXT PRIMARY KEY,
          schema_version TEXT NOT NULL,
          memory_class TEXT NOT NULL,
          modality TEXT NOT NULL,
          retention TEXT NOT NULL,
          importance REAL NOT NULL,
          confidence REAL NOT NULL,
          attributes_json TEXT NOT NULL,
          related_memory_ids_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          FOREIGN KEY(memory_id) REFERENCES local_brain_memories(memory_id));
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO local_brain_meta VALUES('store_id',?)",
                            (uuid.uuid4().hex,))
            self.db.execute("INSERT OR IGNORE INTO local_brain_meta VALUES('contract_version',?)",
                            (CONTRACT_VERSION,))
            self.db.execute("INSERT OR IGNORE INTO local_brain_meta VALUES('clock',?)",
                            (clock,))
        stored = dict(self.db.execute("SELECT key,value FROM local_brain_meta"))
        if stored.get("contract_version") != CONTRACT_VERSION or stored.get("clock") != clock:
            self.close()
            raise ValueError("LOCAL_BRAIN_CLOCK_OR_CONTRACT_CHANGED")
        self.store_id = stored["store_id"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()

    def _time(self, value):
        return timestamp(value, self.clock).isoformat(timespec="microseconds")

    def remember(self, memory):
        if not isinstance(memory, dict):
            raise ValueError("LOCAL_MEMORY_OBJECT_REQUIRED")
        normalized = {
            "memory_id": _text(memory.get("memory_id"), "MEMORY_ID_REQUIRED"),
            "scope": _text(memory.get("scope"), "MEMORY_SCOPE_REQUIRED"),
            "memory_type": _text(memory.get("memory_type"), "MEMORY_TYPE_REQUIRED").upper(),
            "title": _text(memory.get("title"), "MEMORY_TITLE_REQUIRED"),
            "content": _text(memory.get("content"), "MEMORY_CONTENT_REQUIRED"),
            "effective_at": self._time(memory.get("effective_at")),
            "known_at": self._time(memory.get("known_at")),
            "actor": _text(memory.get("actor"), "MEMORY_ACTOR_REQUIRED"),
            "tags": _string_list(memory.get("tags"), "MEMORY_TAGS_LIST_REQUIRED"),
            "entities": _string_list(memory.get("entities"), "MEMORY_ENTITIES_LIST_REQUIRED"),
        }
        if normalized["effective_at"] > normalized["known_at"]:
            raise ValueError("LOCAL_MEMORY_AFTER_RECORDING")
        provenance = memory.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("MEMORY_PROVENANCE_REQUIRED")
        normalized["provenance"] = {
            "source_type": _text(provenance.get("source_type"), "SOURCE_TYPE_REQUIRED"),
            "source_ref": _text(provenance.get("source_ref"), "SOURCE_REF_REQUIRED"),
        }
        supersedes = memory.get("supersedes_memory_id")
        normalized["supersedes_memory_id"] = (
            _text(supersedes, "INVALID_SUPERSEDES_MEMORY_ID") if supersedes else None)
        fingerprint = hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
        profile_requested = any(key in memory for key in PROFILE_FIELDS)
        profile = normalize_profile(memory)
        profile_fingerprint = hashlib.sha256(
            _canonical_json(profile).encode("utf-8")).hexdigest()
        existing = self.db.execute(
            "SELECT revision,fingerprint FROM local_brain_memories WHERE memory_id=?",
            (normalized["memory_id"],)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("LOCAL_MEMORY_ID_COLLISION")
            if profile_requested:
                with self.db:
                    self._record_profile(normalized["memory_id"], profile,
                                         profile_fingerprint)
            return {"memory_id": normalized["memory_id"], "revision": existing["revision"],
                    "disposition": "ALREADY_RECORDED"}
        if normalized["supersedes_memory_id"]:
            target = self.db.execute(
                "SELECT scope FROM local_brain_memories WHERE memory_id=?",
                (normalized["supersedes_memory_id"],)).fetchone()
            if not target:
                raise ValueError("SUPERSEDED_LOCAL_MEMORY_NOT_FOUND")
            if target["scope"] != normalized["scope"]:
                raise ValueError("CROSS_SCOPE_MEMORY_SUPERSESSION_FORBIDDEN")
        with self.db:
            cursor = self.db.execute("""INSERT INTO local_brain_memories(
              memory_id,scope,memory_type,title,content,effective_at,known_at,actor,
              tags_json,entities_json,provenance_json,supersedes_memory_id,fingerprint)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                normalized["memory_id"], normalized["scope"], normalized["memory_type"],
                normalized["title"], normalized["content"], normalized["effective_at"],
                normalized["known_at"], normalized["actor"],
                _canonical_json(normalized["tags"]),
                _canonical_json(normalized["entities"]),
                _canonical_json(normalized["provenance"]),
                normalized["supersedes_memory_id"], fingerprint))
            if profile_requested:
                self._record_profile(normalized["memory_id"], profile,
                                     profile_fingerprint)
        return {"memory_id": normalized["memory_id"], "revision": cursor.lastrowid,
                "disposition": "RECORDED"}

    def _record_profile(self, memory_id, profile, fingerprint):
        existing = self.db.execute(
            "SELECT fingerprint FROM local_brain_memory_profiles WHERE memory_id=?",
            (memory_id,)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("UNIVERSAL_MEMORY_PROFILE_COLLISION")
            return
        self.db.execute("""INSERT INTO local_brain_memory_profiles(
          memory_id,schema_version,memory_class,modality,retention,importance,
          confidence,attributes_json,related_memory_ids_json,fingerprint)
          VALUES(?,?,?,?,?,?,?,?,?,?)""", (
            memory_id, profile["schema_version"], profile["memory_class"],
            profile["modality"], profile["retention"], profile["importance"],
            profile["confidence"], _canonical_json(profile["attributes"]),
            _canonical_json(profile["related_memory_ids"]), fingerprint))

    @staticmethod
    def _memory(row):
        provenance = json.loads(row["provenance_json"])
        profile = default_profile(row["memory_type"], provenance)
        if "profile_schema_version" in row.keys() and row["profile_schema_version"]:
            profile = {"schema_version": row["profile_schema_version"],
                       "memory_class": row["profile_memory_class"],
                       "modality": row["profile_modality"],
                       "retention": row["profile_retention"],
                       "importance": row["profile_importance"],
                       "confidence": row["profile_confidence"],
                       "attributes": json.loads(row["profile_attributes_json"]),
                       "related_memory_ids": json.loads(
                           row["profile_related_memory_ids_json"])}
        return {"revision": row["revision"], "memory_id": row["memory_id"],
                "scope": row["scope"], "memory_type": row["memory_type"],
                "title": row["title"], "content": row["content"],
                "effective_at": row["effective_at"], "known_at": row["known_at"],
                "actor": row["actor"], "tags": json.loads(row["tags_json"]),
                "entities": json.loads(row["entities_json"]),
                "provenance": provenance,
                "supersedes_memory_id": row["supersedes_memory_id"], **profile}

    def _development_memories(self, effective, cutoff, scopes):
        exists = self.db.execute("""SELECT 1 FROM sqlite_master
          WHERE type='table' AND name='development_events'""").fetchone()
        if not exists:
            return []
        parameters = [effective, cutoff]
        where = "effective_at<=? AND known_at<=?"
        if scopes:
            where += " AND project_id IN (" + ",".join("?" for _ in scopes) + ")"
            parameters.extend(scopes)
        rows = self.db.execute(
            f"SELECT * FROM development_events WHERE {where} ORDER BY revision DESC",
            parameters).fetchall()
        superseded = {row["supersedes_event_id"] for row in rows
                      if row["supersedes_event_id"]}
        return [{"revision": row["revision"],
                 "memory_id": "development-event:" + row["event_id"],
                 "scope": row["project_id"], "memory_type": "DEVELOPMENT_EVENT",
                 "title": row["event_type"] + ": " + row["subject_id"],
                 "content": _canonical_json(json.loads(row["payload_json"])),
                 "effective_at": row["effective_at"], "known_at": row["known_at"],
                 "actor": row["actor"],
                 "tags": [row["project_id"], row["event_type"]],
                 "entities": [row["subject_id"]],
                 "provenance": json.loads(row["provenance_json"]),
                 "supersedes_memory_id": None}
                for row in rows if row["event_id"] not in superseded]

    def recall(self, query, *, scopes=None, as_of, knowledge_cutoff=None, limit=8,
               since=None, recent=False):
        """Memories for a query, by relevance, or newest first when `recent` is set.

        "What was I doing?", "what did I do yesterday?" share almost no words with the memories
        that answer them; what answers them is simply what came last. Word relevance alone
        returned month-old notes for exactly that question. So with `recent` the order is time,
        newest first, and words only narrow it when they name something the store knows (a
        title, tag or entity): "what did Kongi do lately" stays about Kongi. Whether a question
        asks about recent events is a language judgement, so the calling model sets `recent`.
        `since` drops anything that happened before it.
        """
        query = _text(query, "RECALL_QUERY_REQUIRED")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("INVALID_RECALL_LIMIT")
        scope_list = _string_list(scopes, "RECALL_SCOPES_LIST_REQUIRED")
        effective = self._time(as_of)
        cutoff = self._time(knowledge_cutoff or as_of)
        parameters = [effective, cutoff]
        where = "effective_at<=? AND known_at<=?"
        if scope_list:
            where += " AND scope IN (" + ",".join("?" for _ in scope_list) + ")"
            parameters.extend(scope_list)
        rows = self.db.execute(f"""SELECT m.*,
            p.schema_version AS profile_schema_version,
            p.memory_class AS profile_memory_class,
            p.modality AS profile_modality,
            p.retention AS profile_retention,
            p.importance AS profile_importance,
            p.confidence AS profile_confidence,
            p.attributes_json AS profile_attributes_json,
            p.related_memory_ids_json AS profile_related_memory_ids_json
          FROM local_brain_memories m
          LEFT JOIN local_brain_memory_profiles p ON p.memory_id=m.memory_id
          WHERE {where} ORDER BY m.revision DESC""", parameters).fetchall()
        memories = [self._memory(row) for row in rows]
        superseded = {item["supersedes_memory_id"] for item in memories
                      if item["supersedes_memory_id"]}
        memories.extend(self._development_memories(effective, cutoff, scope_list))
        floor = self._time(since) if since else None
        query_normalized = _normalized(query)
        query_tokens = _tokens(query)
        matches, everything = [], []
        for memory in memories:
            if memory["memory_id"] in superseded:
                continue
            if floor and memory["effective_at"] < floor:
                continue
            identity = [memory["title"], *memory["tags"], *memory["entities"]]
            body = " ".join([memory["scope"], memory["memory_type"], memory["content"]])
            score, matched_terms, named = 0, [], []
            for item in identity:
                token = _normalized(item)
                if token and token in query_normalized:
                    score += 100
                    named.append(item)
                    matched_terms.append(item)
            body_normalized = _normalized(body)
            for token in query_tokens:
                if token in body_normalized:
                    score += 12
                    matched_terms.append(token)
                elif any(token in _normalized(item) or _normalized(item) in token
                         for item in identity if _normalized(item)):
                    score += 20
                    matched_terms.append(token)
            scored = {"score": score, "matched_terms": list(dict.fromkeys(matched_terms)),
                      "named": named, **memory}
            everything.append(scored)
            if score:
                matches.append(scored)
        if recent:
            named = [item for item in everything if item["named"]]
            pool = named or everything
            pool.sort(key=lambda item: (item["effective_at"], item["revision"]), reverse=True)
            selected = pool[:limit]
        else:
            matches.sort(key=lambda item: (item["score"], item["revision"]), reverse=True)
            selected = matches[:limit]
        if not selected:
            return {"status": "UNKNOWN", "reason": "NO_MATCHING_MEMORY",
                    "query": query, "scopes": scope_list or "ALL_REQUESTED_SCOPES",
                    "scope_completeness": "UNATTESTED", "matches": []}
        return {"status": "KNOWN", "reason": "MEMORY_MATCHES_FOUND",
                "query": query, "scopes": scope_list or "ALL_REQUESTED_SCOPES",
                "scope_completeness": "UNATTESTED",
                "warnings": ["MEMORY_HISTORY_COMPLETENESS_NOT_ATTESTED"],
                "matches": selected}
