"""Persistent, append-only development memory for Graph-MIND dogfooding.

The event log is the source of truth. ``resume_project`` derives a bounded
session-continuity view from events visible at an effective/knowledge cutoff.
It deliberately reports un-attested history instead of inferring absence.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import sqlite3
import uuid

from clock import timestamp


CONTRACT_VERSION = "development-memory-v0.1"
EVENT_TYPES = frozenset({
    "OBJECTIVE_SET",
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_BLOCKED",
    "TASK_CANCELLED",
    "FILE_CHANGED",
    "DECISION_RECORDED",
    "TEST_RECORDED",
    "NEXT_ACTION_SET",
    "NEXT_ACTION_COMPLETED",
    "SESSION_SUMMARY",
    "EXPERIMENT_STARTED",
    "EXPERIMENT_STOPPED",
})
ACTORS = frozenset({"user", "assistant", "system", "tool"})
TEST_STATUSES = frozenset({"PASSED", "FAILED", "SKIPPED", "STOPPED"})
EXPERIMENT_STATUSES = frozenset({"PASSED", "FAILED", "STOPPED"})
EVENT_KEYS = frozenset({
    "event_id", "project_id", "session_id", "event_type", "effective_at",
    "known_at", "actor", "subject_id", "payload", "provenance",
    "supersedes_event_id",
})
REQUIRED_PAYLOAD_FIELDS = {
    "OBJECTIVE_SET": ("objective",),
    "TASK_STARTED": ("title",),
    "TASK_COMPLETED": ("outcome",),
    "TASK_BLOCKED": ("reason",),
    "TASK_CANCELLED": ("reason",),
    "FILE_CHANGED": ("path", "summary", "reason"),
    "DECISION_RECORDED": ("decision", "reason"),
    "TEST_RECORDED": ("name", "status", "summary"),
    "NEXT_ACTION_SET": ("action",),
    "NEXT_ACTION_COMPLETED": ("outcome",),
    "SESSION_SUMMARY": ("summary",),
    "EXPERIMENT_STARTED": ("title",),
    "EXPERIMENT_STOPPED": ("status", "reason"),
}


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _fingerprint(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value, error):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value.strip()


def _portable_path(value):
    raw = _text(value, "FILE_PATH_REQUIRED").replace("\\", "/")
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("/"):
        raise ValueError("PROJECT_RELATIVE_FILE_PATH_REQUIRED")
    path = PurePosixPath(raw)
    if ".." in path.parts or not path.parts:
        raise ValueError("PROJECT_RELATIVE_FILE_PATH_REQUIRED")
    return str(path)


class DevelopmentMemoryStore:
    """SQLite event store and deterministic project-resume projection."""

    def __init__(self, path=":memory:", *, clock="SOURCE_LOCAL"):
        if clock not in {"UTC", "SOURCE_LOCAL"}:
            raise ValueError("INVALID_STORE_CLOCK")
        self.path = path if path == ":memory:" else Path(path)
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.closed = False
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS development_memory_meta(
          key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS development_events(
          revision INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          project_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          effective_at TEXT NOT NULL,
          known_at TEXT NOT NULL,
          actor TEXT NOT NULL,
          subject_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          provenance_json TEXT NOT NULL,
          supersedes_event_id TEXT,
          event_fingerprint TEXT NOT NULL,
          FOREIGN KEY(supersedes_event_id) REFERENCES development_events(event_id));
        CREATE INDEX IF NOT EXISTS development_project_clock
          ON development_events(project_id,known_at,effective_at,revision);
        CREATE INDEX IF NOT EXISTS development_project_type
          ON development_events(project_id,event_type,subject_id,revision);
        CREATE INDEX IF NOT EXISTS development_session
          ON development_events(project_id,session_id,revision);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO development_memory_meta VALUES('store_id',?)",
                            (uuid.uuid4().hex,))
            self.db.execute("INSERT OR IGNORE INTO development_memory_meta VALUES('clock',?)",
                            (clock,))
            self.db.execute("INSERT OR IGNORE INTO development_memory_meta VALUES('contract_version',?)",
                            (CONTRACT_VERSION,))
        self.store_id = self.db.execute(
            "SELECT value FROM development_memory_meta WHERE key='store_id'").fetchone()[0]
        stored_clock = self.db.execute(
            "SELECT value FROM development_memory_meta WHERE key='clock'").fetchone()[0]
        stored_contract = self.db.execute(
            "SELECT value FROM development_memory_meta WHERE key='contract_version'").fetchone()[0]
        if stored_clock != clock or stored_contract != CONTRACT_VERSION:
            self.close()
            raise ValueError("STORE_CLOCK_OR_CONTRACT_CHANGED")
        self.clock = clock

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

    def _validate_event(self, event):
        if not isinstance(event, dict):
            raise ValueError("DEVELOPMENT_EVENT_OBJECT_REQUIRED")
        extra = set(event) - EVENT_KEYS
        if extra:
            raise ValueError("UNSUPPORTED_DEVELOPMENT_EVENT_FIELDS")
        normalized = dict(event)
        for key in ("event_id", "project_id", "session_id", "event_type", "actor", "subject_id"):
            normalized[key] = _text(event.get(key), key.upper() + "_REQUIRED")
        if normalized["event_type"] not in EVENT_TYPES:
            raise ValueError("UNSUPPORTED_DEVELOPMENT_EVENT_TYPE")
        if normalized["actor"] not in ACTORS:
            raise ValueError("UNSUPPORTED_DEVELOPMENT_EVENT_ACTOR")
        effective = self._time(event.get("effective_at"))
        known = self._time(event.get("known_at"))
        if effective > known:
            raise ValueError("DEVELOPMENT_EVENT_AFTER_RECORDING")
        normalized["effective_at"], normalized["known_at"] = effective, known

        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("DEVELOPMENT_EVENT_PAYLOAD_REQUIRED")
        payload = dict(payload)
        for field in REQUIRED_PAYLOAD_FIELDS[normalized["event_type"]]:
            payload[field] = _text(payload.get(field), field.upper() + "_REQUIRED")
        if normalized["event_type"] == "FILE_CHANGED":
            payload["path"] = _portable_path(payload["path"])
        if normalized["event_type"] == "TEST_RECORDED" and payload["status"] not in TEST_STATUSES:
            raise ValueError("INVALID_TEST_STATUS")
        if (normalized["event_type"] == "EXPERIMENT_STOPPED"
                and payload["status"] not in EXPERIMENT_STATUSES):
            raise ValueError("INVALID_EXPERIMENT_STATUS")
        normalized["payload"] = payload

        provenance = event.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("DEVELOPMENT_EVENT_PROVENANCE_REQUIRED")
        provenance = dict(provenance)
        for key in ("source_type", "source_ref"):
            provenance[key] = _text(provenance.get(key), key.upper() + "_REQUIRED")
        digest = provenance.get("content_sha256")
        if digest is not None and (not isinstance(digest, str)
                                   or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("INVALID_PROVENANCE_SHA256")
        normalized["provenance"] = provenance

        supersedes = event.get("supersedes_event_id")
        if supersedes is not None:
            supersedes = _text(supersedes, "INVALID_SUPERSEDES_EVENT_ID")
            if supersedes == normalized["event_id"]:
                raise ValueError("EVENT_CANNOT_SUPERSEDE_ITSELF")
        normalized["supersedes_event_id"] = supersedes
        return normalized

    def _insert(self, event):
        normalized = self._validate_event(event)
        fingerprint = _fingerprint(normalized)
        existing = self.db.execute(
            "SELECT revision,event_fingerprint FROM development_events WHERE event_id=?",
            (normalized["event_id"],)).fetchone()
        if existing:
            if existing["event_fingerprint"] != fingerprint:
                raise ValueError("DEVELOPMENT_EVENT_ID_COLLISION")
            return {"event_id": normalized["event_id"], "revision": existing["revision"],
                    "disposition": "ALREADY_RECORDED"}

        head = self.db.execute(
            "SELECT known_at FROM development_events ORDER BY revision DESC LIMIT 1").fetchone()
        if head and normalized["known_at"] < head["known_at"]:
            raise ValueError("BACKDATED_DEVELOPMENT_KNOWLEDGE_MUTATION")

        required_predecessor = {
            "TASK_COMPLETED": "TASK_STARTED",
            "TASK_BLOCKED": "TASK_STARTED",
            "TASK_CANCELLED": "TASK_STARTED",
            "NEXT_ACTION_COMPLETED": "NEXT_ACTION_SET",
            "EXPERIMENT_STOPPED": "EXPERIMENT_STARTED",
        }.get(normalized["event_type"])
        if required_predecessor:
            predecessor = self.db.execute("""SELECT 1 FROM development_events
              WHERE project_id=? AND subject_id=? AND event_type=? LIMIT 1""",
              (normalized["project_id"], normalized["subject_id"], required_predecessor)).fetchone()
            if not predecessor:
                raise ValueError(required_predecessor + "_NOT_FOUND")
        if normalized["supersedes_event_id"]:
            target = self.db.execute(
                "SELECT project_id,event_type,subject_id FROM development_events WHERE event_id=?",
                (normalized["supersedes_event_id"],)).fetchone()
            if not target:
                raise ValueError("SUPERSEDED_EVENT_NOT_FOUND")
            if target["project_id"] != normalized["project_id"]:
                raise ValueError("CROSS_PROJECT_SUPERSESSION_FORBIDDEN")
            if (target["event_type"] != normalized["event_type"]
                    or target["subject_id"] != normalized["subject_id"]):
                raise ValueError("SUPERSESSION_TYPE_OR_SUBJECT_MISMATCH")
        cursor = self.db.execute("""INSERT INTO development_events(
          event_id,project_id,session_id,event_type,effective_at,known_at,actor,
          subject_id,payload_json,provenance_json,supersedes_event_id,event_fingerprint)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
            normalized["event_id"], normalized["project_id"], normalized["session_id"],
            normalized["event_type"], normalized["effective_at"], normalized["known_at"],
            normalized["actor"], normalized["subject_id"],
            _canonical_json(normalized["payload"]), _canonical_json(normalized["provenance"]),
            normalized["supersedes_event_id"], fingerprint))
        return {"event_id": normalized["event_id"], "revision": cursor.lastrowid,
                "disposition": "RECORDED"}

    def record_event(self, event):
        with self.db:
            return self._insert(event)

    def record_events(self, events):
        if not isinstance(events, list) or not events:
            raise ValueError("NONEMPTY_DEVELOPMENT_EVENT_LIST_REQUIRED")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            results = [self._insert(event) for event in events]
            self.db.commit()
            return results
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def _event(row):
        return {"revision": row["revision"], "event_id": row["event_id"],
                "project_id": row["project_id"], "session_id": row["session_id"],
                "event_type": row["event_type"], "effective_at": row["effective_at"],
                "known_at": row["known_at"], "actor": row["actor"],
                "subject_id": row["subject_id"], "payload": json.loads(row["payload_json"]),
                "provenance": json.loads(row["provenance_json"]),
                "supersedes_event_id": row["supersedes_event_id"]}

    def events(self, project_id, *, as_of, knowledge_cutoff=None):
        project_id = _text(project_id, "PROJECT_ID_REQUIRED")
        effective = self._time(as_of)
        cutoff = self._time(knowledge_cutoff or as_of)
        rows = self.db.execute("""SELECT * FROM development_events
          WHERE project_id=? AND effective_at<=? AND known_at<=?
          ORDER BY effective_at,known_at,revision""", (project_id, effective, cutoff)).fetchall()
        events = [self._event(row) for row in rows]
        superseded = {e["supersedes_event_id"] for e in events if e["supersedes_event_id"]}
        return [e for e in events if e["event_id"] not in superseded]

    def resume_project(self, project_id, *, as_of, knowledge_cutoff=None,
                       recent_file_limit=10, recent_test_limit=10):
        if type(recent_file_limit) is not int or recent_file_limit < 1:
            raise ValueError("INVALID_RECENT_FILE_LIMIT")
        if type(recent_test_limit) is not int or recent_test_limit < 1:
            raise ValueError("INVALID_RECENT_TEST_LIMIT")
        selected = self.events(project_id, as_of=as_of,
                               knowledge_cutoff=knowledge_cutoff)
        cutoff = self._time(knowledge_cutoff or as_of)
        effective = self._time(as_of)
        if not selected:
            return {"status": "UNKNOWN", "reason": "PROJECT_MEMORY_NOT_FOUND",
                    "project_id": project_id, "as_of": effective,
                    "knowledge_cutoff": cutoff,
                    "scope_completeness": "UNATTESTED", "evidence": []}

        objective = None
        tasks, files, decisions, next_actions, experiments = {}, {}, {}, {}, {}
        tests, sessions = [], []
        event_by_id = {e["event_id"]: e for e in selected}

        for event in selected:
            kind, subject, payload = event["event_type"], event["subject_id"], event["payload"]
            if kind == "OBJECTIVE_SET":
                objective = {"value": payload["objective"],
                             "evidence_event_ids": [event["event_id"]]}
            elif kind == "TASK_STARTED":
                old = tasks.get(subject, {})
                tasks[subject] = {"task_id": subject, "title": payload["title"],
                                  "status": "OPEN", "detail": None,
                                  "last_revision": event["revision"],
                                  "evidence_event_ids": old.get("evidence_event_ids", []) + [event["event_id"]]}
            elif kind in {"TASK_COMPLETED", "TASK_BLOCKED", "TASK_CANCELLED"}:
                old = tasks.get(subject, {"task_id": subject, "title": subject,
                                          "evidence_event_ids": []})
                status = {"TASK_COMPLETED": "COMPLETED", "TASK_BLOCKED": "BLOCKED",
                          "TASK_CANCELLED": "CANCELLED"}[kind]
                detail = payload.get("outcome") or payload.get("reason")
                tasks[subject] = {**old, "status": status, "detail": detail,
                                  "last_revision": event["revision"],
                                  "evidence_event_ids": old["evidence_event_ids"] + [event["event_id"]]}
            elif kind == "FILE_CHANGED":
                files[payload["path"]] = {"path": payload["path"],
                    "summary": payload["summary"], "reason": payload["reason"],
                    "last_revision": event["revision"],
                    "evidence_event_ids": [event["event_id"]]}
            elif kind == "DECISION_RECORDED":
                decisions[subject] = {"decision_id": subject, "decision": payload["decision"],
                    "reason": payload["reason"], "last_revision": event["revision"],
                    "evidence_event_ids": [event["event_id"]]}
            elif kind == "TEST_RECORDED":
                tests.append({"test_id": subject, "name": payload["name"],
                    "status": payload["status"], "summary": payload["summary"],
                    "last_revision": event["revision"],
                    "evidence_event_ids": [event["event_id"]]})
            elif kind == "NEXT_ACTION_SET":
                old = next_actions.get(subject, {})
                next_actions[subject] = {"action_id": subject, "action": payload["action"],
                    "status": "OPEN", "last_revision": event["revision"],
                    "evidence_event_ids": old.get("evidence_event_ids", []) + [event["event_id"]]}
            elif kind == "NEXT_ACTION_COMPLETED":
                old = next_actions.get(subject, {"action_id": subject, "action": subject,
                                                 "evidence_event_ids": []})
                next_actions[subject] = {**old, "status": "COMPLETED",
                    "outcome": payload["outcome"], "last_revision": event["revision"],
                    "evidence_event_ids": old["evidence_event_ids"] + [event["event_id"]]}
            elif kind == "EXPERIMENT_STARTED":
                old = experiments.get(subject, {})
                experiments[subject] = {"experiment_id": subject, "title": payload["title"],
                    "status": "RUNNING", "last_revision": event["revision"],
                    "evidence_event_ids": old.get("evidence_event_ids", []) + [event["event_id"]]}
            elif kind == "EXPERIMENT_STOPPED":
                old = experiments.get(subject, {"experiment_id": subject, "title": subject,
                                                "evidence_event_ids": []})
                experiments[subject] = {**old, "status": payload["status"],
                    "reason": payload["reason"], "last_revision": event["revision"],
                    "evidence_event_ids": old["evidence_event_ids"] + [event["event_id"]]}
            elif kind == "SESSION_SUMMARY":
                sessions.append({"session_id": subject, "summary": payload["summary"],
                    "last_revision": event["revision"],
                    "evidence_event_ids": [event["event_id"]]})

        open_tasks = sorted((v for v in tasks.values() if v["status"] in {"OPEN", "BLOCKED"}),
                            key=lambda v: v["last_revision"], reverse=True)
        current_task = open_tasks[0] if open_tasks else None
        open_actions = sorted((v for v in next_actions.values() if v["status"] == "OPEN"),
                              key=lambda v: v["last_revision"], reverse=True)
        active_experiments = sorted((v for v in experiments.values() if v["status"] == "RUNNING"),
                                    key=lambda v: v["last_revision"], reverse=True)
        recent_files = sorted(files.values(), key=lambda v: v["last_revision"], reverse=True)[:recent_file_limit]
        recent_tests = sorted(tests, key=lambda v: v["last_revision"], reverse=True)[:recent_test_limit]
        decision_list = sorted(decisions.values(), key=lambda v: v["last_revision"], reverse=True)
        last_session = max(sessions, key=lambda v: v["last_revision"]) if sessions else None

        projected = [objective, current_task, *open_tasks, *open_actions, *active_experiments,
                     *recent_files, *recent_tests, *decision_list, last_session]
        evidence_ids = []
        for item in projected:
            if item:
                evidence_ids.extend(item.get("evidence_event_ids", []))
        evidence_ids = list(dict.fromkeys(evidence_ids))
        evidence = [event_by_id[event_id] for event_id in evidence_ids if event_id in event_by_id]
        evidence.sort(key=lambda e: e["revision"])

        return {"status": "KNOWN", "reason": "PROJECT_MEMORY_RECONSTRUCTED",
                "project_id": project_id, "as_of": effective, "knowledge_cutoff": cutoff,
                "scope_completeness": "UNATTESTED",
                "warnings": ["PROJECT_HISTORY_COMPLETENESS_NOT_ATTESTED"],
                "objective": objective, "current_task": current_task,
                "open_tasks": open_tasks, "next_actions": open_actions,
                "active_experiments": active_experiments, "recent_files": recent_files,
                "recent_tests": recent_tests, "decisions": decision_list,
                "last_session": last_session, "evidence_event_ids": evidence_ids,
                "evidence": evidence}

    def explain_decision(self, project_id, decision_id, *, as_of,
                         knowledge_cutoff=None):
        project_id = _text(project_id, "PROJECT_ID_REQUIRED")
        decision_id = _text(decision_id, "DECISION_ID_REQUIRED")
        effective = self._time(as_of)
        cutoff = self._time(knowledge_cutoff or as_of)
        rows = self.db.execute("""SELECT * FROM development_events
          WHERE project_id=? AND subject_id=? AND event_type='DECISION_RECORDED'
          AND effective_at<=? AND known_at<=?
          ORDER BY effective_at,known_at,revision""",
          (project_id, decision_id, effective, cutoff)).fetchall()
        history = [self._event(row) for row in rows]
        if not history:
            return {"status": "UNKNOWN", "reason": "DECISION_NOT_FOUND",
                    "project_id": project_id, "decision_id": decision_id,
                    "as_of": effective, "knowledge_cutoff": cutoff,
                    "scope_completeness": "UNATTESTED", "history": []}
        superseded = {event["supersedes_event_id"] for event in history
                      if event["supersedes_event_id"]}
        active = [event for event in history if event["event_id"] not in superseded]
        if len(active) != 1:
            return {"status": "UNKNOWN", "reason": "CONFLICTING_ACTIVE_DECISIONS",
                    "project_id": project_id, "decision_id": decision_id,
                    "as_of": effective, "knowledge_cutoff": cutoff,
                    "scope_completeness": "UNATTESTED",
                    "evidence_event_ids": [event["event_id"] for event in active],
                    "history": history}
        current = active[0]
        return {"status": "KNOWN", "reason": "DECISION_RECONSTRUCTED",
                "project_id": project_id, "decision_id": decision_id,
                "as_of": effective, "knowledge_cutoff": cutoff,
                "scope_completeness": "UNATTESTED",
                "decision": current["payload"]["decision"],
                "decision_reason": current["payload"]["reason"],
                "evidence_event_ids": [current["event_id"]],
                "provenance": current["provenance"], "history": history}
