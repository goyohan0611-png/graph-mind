"""Incremental, redacted conversation capture for the user-owned Local Brain."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import collections
import hashlib
import json
import math
import os
import re
import sqlite3
import time

from graph_mind.clock import timestamp
from graph_mind.forget import erase
from graph_mind.paths import use_wal

CAPTURE_VERSION = "codex-conversation-capture-v0.1"
SECRET_PATTERNS = (
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"\bkey_[A-Za-z0-9_-]{10,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_ -]?key|secret|token)\s*[:=]\s*)[^\s]+"),
     r"\1[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
     "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    # a database URL carries its password: postgres://user:PASSWORD@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1[REDACTED]\2"),
    # a password written after its name: "password: x", "pwd=x", "비밀번호: x", "비번=x"
    (re.compile(r"(?i)((?<![a-z])(?:password|passwd|pwd)\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"((?:비밀번호|비번|암호)\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
    # ... or in a sentence: "my password is x", "비밀번호는 x", "와이파이 비번 x". Only a value
    # with a digit or a symbol, so "the password is stored in the vault" stays as it is; a
    # Korean ending glued to it ("1234야") is kept
    (re.compile(r"(?i)((?<![a-z])(?:password|passwd|passcode|pin)\s+(?:is|was)\s+)"
                r"(?!1Password)(?=\S*[\d!@#$%^&*])[^\s,]+"), r"\1[REDACTED]"),
    (re.compile(r"((?:비밀번호|비번|암호|패스워드)(?:는|은|가|이)?\s*)(?!1Password)"
                r"(?=[^\s가-힣]*[\d!@#$%^&*])[^\s가-힣,]{4,}"), r"\1[REDACTED]"),
    (re.compile(r"\b[srp]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[REDACTED_STRIPE_KEY]"),
    # a Korean resident registration number
    (re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)"), "[REDACTED_ID_NUMBER]"),
    # a card number, only when its check digit is right (not every long number is a card)
    # and the shape cards have (15-16 digits from 3-6): on LongMemEval's 189,515 turns a looser
    # rule took 5 long order and tracking numbers for cards
    (re.compile(r"(?<![\d-])[3-6]\d{3}(?:[ -]?\d{4}){2}[ -]?\d{3,4}(?![\d-])"),
     lambda m: "[REDACTED_CARD]" if _luhn(m.group(0)) else m.group(0)),
)


def _luhn(text):
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2)
                for i, d in enumerate(reversed(digits)))
    return total % 10 == 0
# A token no pattern above knows: long, machine-random (mixed case and digits, no word-shaped
# pieces, high character entropy). Measured: catches 91% of random 20-64 character tokens; in
# LongMemEval's 246,750 chat turns it fires 92 times, nearly all on real ids and tokens (an
# OAuth token, Drive file ids, ad click ids); in this repository's code and docs, never.
# Commit ids, hashes and UUIDs are left alone: those get recalled on purpose.
RANDOM_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_\-])")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
WORD_PIECE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _looks_random(token):
    if UUID.fullmatch(token):
        return False
    core = re.sub(r"[_\-]", "", token)
    if len(core) < 18 or re.fullmatch(r"[0-9a-fA-F]+", core):
        return False
    if not (any(c.isupper() for c in core) and any(c.islower() for c in core)
            and sum(c.isdigit() for c in core) >= 2):
        return False
    pieces = WORD_PIECE.findall(core)           # code names are words strung together
    if pieces and sum(map(len, pieces)) / len(pieces) >= 3.0:
        return False
    counts = collections.Counter(core)
    return -sum(n / len(core) * math.log2(n / len(core)) for n in counts.values()) >= 3.7
AUTOMATIC_BLOCKS = re.compile(
    r"<(?:environment_context|recommended_plugins|in-app-browser-context)\b[^>]*>.*?"
    r"</(?:environment_context|recommended_plugins|in-app-browser-context)>",
    flags=re.DOTALL,
)


def _text(value, error):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value.strip()


# Codex opens each task by injecting the workspace's AGENTS.md as a user message. It is the
# harness talking, not the person, and it is long enough to fill a context packet on its own.
AGENT_INSTRUCTIONS = re.compile(
    r"(?:#\s*AGENTS\.md instructions[^\n]*\s*)?<INSTRUCTIONS>.*?</INSTRUCTIONS>", flags=re.DOTALL)


def masking_on():
    """On unless ~/.graph-mind/config.json says "mask_secrets": false: someone running a model on
    their own machines may want keys kept in memory verbatim."""
    from graph_mind.sharing import _config
    return _config().get("mask_secrets", True) is not False


def _redact(value):
    cleaned = AGENT_INSTRUCTIONS.sub("", AUTOMATIC_BLOCKS.sub("", value))
    if not masking_on():
        return cleaned.strip(), 0
    count = 0
    for pattern, replacement in SECRET_PATTERNS:
        before = cleaned.count("[REDACTED")
        cleaned = pattern.sub(replacement, cleaned)
        count += cleaned.count("[REDACTED") - before     # a number that is no card: not one
    found = [0]

    def unknown_token(match):
        if not _looks_random(match.group(0)):
            return match.group(0)
        found[0] += 1
        return "[REDACTED_SECRET]"
    cleaned = RANDOM_CANDIDATE.sub(unknown_token, cleaned)
    return cleaned.strip(), count + found[0]


def _message_text(payload):
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text", "text"}:
            value = item.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


class ConversationMemoryStore:
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
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_turns(
          revision INTEGER PRIMARY KEY AUTOINCREMENT,
          turn_id TEXT NOT NULL UNIQUE,
          client TEXT NOT NULL,
          client_session_id TEXT NOT NULL,
          scope TEXT NOT NULL,
          cwd TEXT,
          happened_at TEXT NOT NULL,
          known_at TEXT NOT NULL,
          role TEXT NOT NULL,
          content_redacted TEXT NOT NULL,
          raw_sha256 TEXT NOT NULL,
          redaction_count INTEGER NOT NULL,
          source_path TEXT NOT NULL,
          source_ordinal INTEGER NOT NULL,
          fingerprint TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS conversation_scope_time
          ON conversation_turns(scope,happened_at DESC);
        CREATE INDEX IF NOT EXISTS conversation_session_time
          ON conversation_turns(client_session_id,happened_at);
        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_turns_fts USING fts5(
          content_redacted, content='conversation_turns', content_rowid='revision',
          tokenize='unicode61');
        CREATE TRIGGER IF NOT EXISTS conversation_turns_ai AFTER INSERT ON conversation_turns BEGIN
          INSERT INTO conversation_turns_fts(rowid,content_redacted)
          VALUES(new.revision,new.content_redacted);
        END;
        """)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()

    def _time(self, value):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                if self.clock == "SOURCE_LOCAL" and parsed.utcoffset() is not None:
                    value = parsed.astimezone().replace(tzinfo=None).isoformat(
                        timespec="microseconds")
        return timestamp(value, self.clock).isoformat(timespec="microseconds")

    def record(self, turn):
        required = ("turn_id", "client", "client_session_id", "scope", "role",
                    "content_redacted", "raw_sha256", "source_path")
        normalized = {key: _text(turn.get(key), key.upper() + "_REQUIRED")
                      for key in required}
        normalized["cwd"] = turn.get("cwd")
        normalized["happened_at"] = self._time(turn.get("happened_at"))
        normalized["known_at"] = self._time(turn.get("known_at"))
        normalized["source_ordinal"] = int(turn.get("source_ordinal"))
        normalized["redaction_count"] = int(turn.get("redaction_count", 0))
        fingerprint = hashlib.sha256(json.dumps(normalized, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        existing = self.db.execute(
            "SELECT revision,fingerprint,raw_sha256 FROM conversation_turns WHERE turn_id=?",
            (normalized["turn_id"],)).fetchone()
        if existing:
            # The id already binds the session, the record and the hash of what was said, so the
            # same id with the same text is the same turn seen again, even if this read happened
            # at another time. Only the same id carrying different text is a real collision.
            if existing["raw_sha256"] != normalized["raw_sha256"]:
                raise ValueError("CONVERSATION_TURN_ID_COLLISION")
            return {"turn_id": normalized["turn_id"], "revision": existing["revision"],
                    "disposition": "ALREADY_RECORDED"}
        with self.db:
            cursor = self.db.execute("""INSERT INTO conversation_turns(
              turn_id,client,client_session_id,scope,cwd,happened_at,known_at,role,
              content_redacted,raw_sha256,redaction_count,source_path,source_ordinal,fingerprint)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                normalized["turn_id"], normalized["client"],
                normalized["client_session_id"], normalized["scope"], normalized["cwd"],
                normalized["happened_at"], normalized["known_at"], normalized["role"],
                normalized["content_redacted"], normalized["raw_sha256"],
                normalized["redaction_count"], normalized["source_path"],
                normalized["source_ordinal"], fingerprint))
        return {"turn_id": normalized["turn_id"], "revision": cursor.lastrowid,
                "disposition": "RECORDED"}

    def search(self, query, *, scopes=None, limit=10):
        query = _text(query, "CONVERSATION_QUERY_REQUIRED")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("INVALID_CONVERSATION_QUERY_LIMIT")
        terms = list(dict.fromkeys(re.findall(r"[^\W_]{2,}", query.casefold())))
        if not terms:
            return {"status": "UNKNOWN", "reason": "NO_SEARCHABLE_QUERY_TERMS",
                    "scope_completeness": "UNATTESTED", "turns": []}
        # Prefix terms, not exact ones: Korean glues particles to the noun, so the index holds
        # "콩이를" and "제주도에" as single tokens and an exact "콩이" matched neither.
        expression = " OR ".join('"' + term.replace('"', '""') + '"*' for term in terms)
        where, parameters = ["conversation_turns_fts MATCH ?"], [expression]
        if scopes:
            where.append("t.scope IN (" + ",".join("?" for _ in scopes) + ")")
            parameters.extend(scopes)
        parameters.append(limit)
        rows = self.db.execute("""SELECT t.*,bm25(conversation_turns_fts) AS rank
          FROM conversation_turns_fts JOIN conversation_turns t
          ON t.revision=conversation_turns_fts.rowid WHERE """
          + " AND ".join(where) + " ORDER BY rank,t.happened_at DESC LIMIT ?",
          parameters).fetchall()
        turns = [self._turn(row) for row in rows]
        return {"status": "KNOWN" if turns else "UNKNOWN",
                "reason": "CONVERSATION_TURNS_FOUND" if turns else "NO_MATCHING_TURN",
                "scope_completeness": "UNATTESTED", "turns": turns}

    def latest(self, *, scopes=None, since=None, limit=10):
        """The most recent turns, newest first: what "what was I just doing?" is asking for."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("INVALID_CONVERSATION_QUERY_LIMIT")
        where, parameters = ["1=1"], []
        if scopes:
            where.append("scope IN (" + ",".join("?" for _ in scopes) + ")")
            parameters.extend(scopes)
        if since:
            where.append("happened_at>=?")
            parameters.append(self._time(since))
        rows = self.db.execute("SELECT * FROM conversation_turns WHERE " + " AND ".join(where)
                               + " ORDER BY happened_at DESC, revision DESC LIMIT ?",
                               [*parameters, limit]).fetchall()
        return {"status": "KNOWN" if rows else "UNKNOWN",
                "reason": "RECENT_TURNS" if rows else "NO_TURNS_IN_RANGE",
                "scope_completeness": "UNATTESTED", "turns": [self._turn(row) for row in rows]}

    @staticmethod
    def _turn(row):
        return {"turn_id": row["turn_id"], "client": row["client"],
                "client_session_id": row["client_session_id"], "scope": row["scope"],
                "cwd": row["cwd"], "happened_at": row["happened_at"],
                "role": row["role"], "content": row["content_redacted"],
                "redaction_count": row["redaction_count"],
                "source_path": row["source_path"], "source_ordinal": row["source_ordinal"]}


class CodexConversationCapture:
    """Read only newly appended complete JSONL records from Codex sessions."""

    CLIENT = "codex"

    def __init__(self, sessions_root, store, *, state_path, workspace_scopes=None,
                 now=None, publish=None, forget=None):
        self.sessions_root = Path(sessions_root).resolve()
        if not self.sessions_root.is_dir():
            raise ValueError("CODEX_SESSIONS_ROOT_NOT_FOUND")
        if not isinstance(store, ConversationMemoryStore):
            raise ValueError("CONVERSATION_MEMORY_STORE_REQUIRED")
        self.store = store
        self.state_path = Path(state_path).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_scopes = {str(Path(key).resolve()): value
                                 for key, value in (workspace_scopes or {}).items()}
        self.now = now or (lambda: datetime.now().isoformat(timespec="microseconds"))
        self.publish = publish            # called with each turn that is new to this store
        # erases captured turns everywhere (forget.forget_ids with the shared log); by default
        # this store only
        self.forget = forget or self._forget_here

    def _forget_here(self, turn_ids):
        erase(self.store.path, {"turn_ids": turn_ids})

    FORGET_TOOL = "brain_forget"
    FORGET_WINDOW_MINUTES = 30

    def _forget_call(self, item):
        """Whether this record is the assistant calling brain_forget. Codex: a function call
        named for it, or (code mode) a completed MCP tool call of it."""
        payload = item.get("payload") or {}
        if payload.get("type") in {"function_call", "custom_tool_call"}:
            return str(payload.get("name", "")).endswith(self.FORGET_TOOL)
        call = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        return call.get("type") == "McpToolCall" and call.get("tool") == self.FORGET_TOOL

    def _forget_exchange(self, session_id, at, sessions):
        """The assistant called brain_forget: what led up to it is a conversation about deleting,
        so it goes too. From the user turn that asked (or from the previous forget call, when this
        is the second, deleting call of the same exchange) up to now; the assistant's reply that
        follows is skipped until the user speaks again."""
        at = self.store._time(at)
        previous = sessions.get(session_id, {}).get("signal_at")
        db = self.store.db
        if previous and (datetime.fromisoformat(at) - datetime.fromisoformat(previous)
                         ).total_seconds() <= self.FORGET_WINDOW_MINUTES * 60:
            since = previous
        else:
            since = db.execute(
                "SELECT max(happened_at) FROM conversation_turns WHERE client=? AND "
                "client_session_id=? AND role='user' AND happened_at<=?",
                (self.CLIENT, session_id, at)).fetchone()[0]
        if since:
            ids = [row[0] for row in db.execute(
                "SELECT turn_id FROM conversation_turns WHERE client=? AND client_session_id=? "
                "AND happened_at>=?", (self.CLIENT, session_id, since))]
            if ids:
                self.forget(ids)
        sessions[session_id] = {"signal_at": at, "quiet": True}

    def _files(self):
        return sorted(self.sessions_root.rglob("*.jsonl"))

    def _session_meta(self, path):
        session_id, cwd = path.stem, None
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                first = json.loads(handle.readline())
            payload = first.get("payload") or {}
            session_id = str(payload.get("session_id") or payload.get("id") or session_id)
            cwd = payload.get("cwd")
        except (OSError, ValueError, TypeError):
            pass
        resolved_cwd = str(Path(cwd).resolve()) if isinstance(cwd, str) and cwd else None
        scope = self.workspace_scopes.get(resolved_cwd, "global")
        return session_id, cwd, scope

    def _identity(self, item, ordinal):
        """What makes this record the same record on every scan. Codex numbers its records."""
        return ordinal

    def _message(self, item):
        """(role, text) when this record is something a person or the assistant said, else None."""
        payload = item.get("payload") or {}
        if (item.get("type") != "response_item" or payload.get("type") != "message"
                or payload.get("role") not in {"user", "assistant"}):
            return None
        return payload["role"], _message_text(payload)

    def initialize_baseline(self):
        files = {str(path): path.stat().st_size for path in self._files()}
        self._write_state(files)
        return {"status": "BASELINE_CREATED", "file_count": len(files),
                "state_path": str(self.state_path)}

    def _load_state(self):
        if not self.state_path.exists():
            raise ValueError("CONVERSATION_CAPTURE_BASELINE_REQUIRED")
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if value.get("capture_version") != CAPTURE_VERSION:
            raise ValueError("CONVERSATION_CAPTURE_VERSION_MISMATCH")
        self._forget_sessions = value.get("forget_sessions", {})
        return value.get("offsets", {})

    def _write_state(self, offsets):
        value = {"capture_version": CAPTURE_VERSION,
                 "sessions_root": str(self.sessions_root), "offsets": offsets,
                 "forget_sessions": getattr(self, "_forget_sessions", {})}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.state_path)

    def scan(self):
        offsets = self._load_state()
        new_offsets = dict(offsets)
        recorded, scanned_rows, redactions = [], 0, 0
        known_at = self.now()
        for path in self._files():
            key = str(path)
            start = int(offsets.get(key, 0))
            size = path.stat().st_size
            if size < start:
                start = 0
            session_id, cwd, scope = self._session_meta(path)
            with path.open("rb") as handle:
                handle.seek(start)
                committed = start
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    committed = handle.tell()
                    scanned_rows += 1
                    try:
                        item = json.loads(line.decode("utf-8", "strict"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if self._forget_call(item):
                        self._forget_exchange(session_id, item.get("timestamp") or known_at,
                                              self._forget_sessions)
                        continue
                    message = self._message(item)
                    if message is None:
                        continue
                    role, raw = message
                    if not raw.strip():
                        continue
                    quiet = self._forget_sessions.get(session_id)
                    if quiet and quiet.get("quiet"):
                        if role == "assistant":
                            continue           # the reply to a forget call: not remembered
                        quiet["quiet"] = False  # the user spoke again: the exchange is over
                    cleaned, count = _redact(raw)
                    if not cleaned:
                        continue
                    redactions += count
                    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    ordinal_value = item.get("ordinal")
                    # where the line sits in the file: unlike the row count of this one scan,
                    # it is the same every time the file is read
                    ordinal = int(ordinal_value) if ordinal_value is not None else line_start
                    binding = (f"{session_id}\0{self._identity(item, ordinal)}\0{raw_hash}"
                               .encode("utf-8"))
                    turn = {"turn_id": f"{self.CLIENT}-turn-"
                                       + hashlib.sha256(binding).hexdigest(),
                        "client": self.CLIENT, "client_session_id": session_id,
                        "scope": scope, "cwd": cwd,
                        "happened_at": item.get("timestamp") or known_at,
                        "known_at": known_at, "role": role,
                        "content_redacted": cleaned, "raw_sha256": raw_hash,
                        "redaction_count": count, "source_path": key,
                        "source_ordinal": ordinal}
                    outcome = self.store.record(turn)
                    if self.publish and outcome.get("disposition") != "ALREADY_RECORDED":
                        try:
                            self.publish(turn)
                        except Exception:
                            # ponytail: the hub is unreachable; the turn is kept on this PC and
                            # brain_folder(share_existing=true) sends it to the others later
                            pass
                    recorded.append(outcome)
                new_offsets[key] = committed
        self._write_state(new_offsets)
        return {"status": "TURNS_RECORDED" if recorded else "NO_NEW_TURNS",
                "recorded_turns": len(recorded), "scanned_rows": scanned_rows,
                "redactions": redactions, "record_results": recorded}


CLAUDE_CODE_BLOCKS = re.compile(
    r"<(system-reminder|task-notification|local-command-caveat|local-command-stdout|"
    r"command-name|command-message|command-args)\b[^>]*>.*?</\1>", flags=re.DOTALL)


class ClaudeCodeConversationCapture(CodexConversationCapture):
    """Read only newly appended complete JSONL records from Claude Code transcripts.

    A transcript holds far more than the conversation: tool calls, tool results, the model's
    thinking, attachments, mode switches. Only two kinds of record are something somebody SAID — a
    user turn typed as text, and an assistant text block — and only those are kept, verbatim.
    Harness notices injected into user turns (system reminders, task notifications, command
    echoes) are stripped, and sub-agent side chains are skipped: the user did not have that
    conversation. Secrets are masked by the same rules as every other capture.
    """

    CLIENT = "claude-code"

    def _identity(self, item, ordinal):
        """Claude Code records carry no ordinal, and the row count of one scan is not stable: a
        re-read from the start would give every turn a new id and store it twice. Each record's
        own uuid is."""
        return item.get("uuid") or ordinal

    def _session_meta(self, path):
        session_id, cwd = path.stem, None
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    record = json.loads(line)
                    if record.get("cwd"):
                        session_id = str(record.get("sessionId") or session_id)
                        cwd = record["cwd"]
                        break
        except (OSError, ValueError, TypeError):
            pass
        resolved_cwd = str(Path(cwd).resolve()) if isinstance(cwd, str) and cwd else None
        return session_id, cwd, self.workspace_scopes.get(resolved_cwd, "global")

    def _forget_call(self, item):
        """An assistant record with a tool_use block calling brain_forget (any MCP prefix)."""
        if item.get("type") != "assistant" or item.get("isSidechain"):
            return False
        content = (item.get("message") or {}).get("content")
        return isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            and str(block.get("name", "")).endswith(self.FORGET_TOOL) for block in content)

    def _message(self, item):
        if item.get("isSidechain") or item.get("isMeta"):
            return None
        queued = item.get("attachment") if item.get("type") == "attachment" else None
        if isinstance(queued, dict) and queued.get("type") == "queued_command":
            # A message typed while the assistant was still working is not a "user" record: it
            # is queued and delivered as an attachment. Missing it loses exactly the corrections
            # and asides people make mid-task. (The matching queue-operation records are the
            # queue's bookkeeping of the same text and are skipped to avoid storing it twice.)
            # Only what the person typed: background-task notifications queue the same way.
            if queued.get("humanTurn") is False or queued.get("commandMode") != "prompt":
                return None
            prompt = queued.get("prompt")
            if isinstance(prompt, list):                # a pasted image arrives as blocks
                prompt = "\n".join(block.get("text", "") for block in prompt
                                   if isinstance(block, dict) and block.get("type") == "text")
            if not isinstance(prompt, str):
                return None
            return "user", CLAUDE_CODE_BLOCKS.sub("", prompt)
        if item.get("type") not in {"user", "assistant"}:
            return None
        message = item.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):    # text blocks only: no tool_use, tool_result, thinking
            text = "\n".join(block.get("text", "") for block in content
                             if isinstance(block, dict) and block.get("type") == "text")
        else:
            return None
        return item["type"], CLAUDE_CODE_BLOCKS.sub("", text)


class ClaudeCoworkConversationCapture(ClaudeCodeConversationCapture):
    """Claude desktop's Cowork mode: the same agent and transcript format as Claude Code, but each
    session keeps its own .claude/projects inside local-agent-mode-sessions. Only those
    transcripts are read; the session folders also hold credentials and uploads, never opened."""

    CLIENT = "claude-cowork"

    def _files(self):
        # Cowork transcript paths run to ~500 characters; past Windows' 260 a plain rglob skips
        # them without an error (5 of 68 found). The extended-length prefix lifts the limit.
        # ponytail: only here; the Codex and Claude Code roots stay short and keep cursor keys.
        root = self.sessions_root
        if os.name == "nt" and not str(root).startswith("\\\\?\\"):
            root = Path("\\\\?\\" + str(root))
        return sorted(path for path in root.rglob("*.jsonl")
                      if ".claude" in path.parts and "projects" in path.parts
                      and "subagents" not in path.parts)


class OpenCodeConversationCapture(ClaudeCodeConversationCapture):
    """OpenCode keeps no transcript file of its own that is meant to be read; Graph-MIND's plugin
    for it (opencode_plugin.js) appends what was said, in Claude Code's record shape, one file per
    session under ~/.graph-mind/opencode. Read the same way."""

    CLIENT = "opencode"

    def _files(self):
        return sorted(self.sessions_root.glob("*.jsonl"))

    def initialize_baseline(self):
        """From the start, not from now: every line here was written by the plugin since it was
        installed, and the folder first appears with the first message, which a baseline at the
        end of the files would skip."""
        self._write_state({})
        return {"status": "BASELINE_CREATED", "file_count": 0, "state_path": str(self.state_path)}

    DONE_AFTER = 3600          # seconds without a new line: the session is over

    def scan(self):
        """Then delete the files Graph-MIND's own plugin and hooks wrote once every line is
        stored: they hold what was typed before masking, so a password masked in memory, or
        deleted with brain_forget, stayed on disk in them. A session that goes on later starts
        a new file, read from its start."""
        result = super().scan()
        offsets, now, removed = self._load_state(), time.time(), False
        for path in self._files():
            try:
                info = path.stat()
                if (offsets.get(str(path)) == info.st_size
                        and now - info.st_mtime > self.DONE_AFTER):
                    path.unlink()
                    offsets.pop(str(path))
                    removed = True
            except OSError:
                continue                   # still being written, or gone already
        if removed:
            self._write_state(offsets)
        return result


class CopilotConversationCapture(CodexConversationCapture):
    """GitHub Copilot CLI: one events.jsonl per session under ~/.copilot/session-state/<id>/.
    Only user.message (what was typed, not the CLI's transformedContent with its injected
    datetime and reminders) and assistant.message text are kept; hooks, tool calls and usage
    records are skipped."""

    CLIENT = "copilot-cli"

    def _files(self):
        return sorted(self.sessions_root.glob("*/events.jsonl"))

    def _session_meta(self, path):
        cwd = None
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                first = json.loads(handle.readline())
            cwd = ((first.get("data") or {}).get("context") or {}).get("cwd")
        except (OSError, ValueError, TypeError):
            pass
        resolved_cwd = str(Path(cwd).resolve()) if isinstance(cwd, str) and cwd else None
        return path.parent.name, cwd, self.workspace_scopes.get(resolved_cwd, "global")

    def _identity(self, item, ordinal):
        return item.get("id") or ordinal

    def _forget_call(self, item):
        if item.get("type") != "assistant.message":
            return False
        return any(str((request or {}).get("name", "")).endswith(self.FORGET_TOOL)
                   for request in (item.get("data") or {}).get("toolRequests") or [])

    def _message(self, item):
        role = {"user.message": "user", "assistant.message": "assistant"}.get(item.get("type"))
        text = (item.get("data") or {}).get("content")
        return (role, text) if role and isinstance(text, str) else None


CURSOR_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
# "Sunday, Oct 18, 2026, 2:46 PM (UTC+9)": the only time a Cursor transcript carries
CURSOR_TIME = re.compile(r"<timestamp>\s*\w+, (\w{3} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M)"
                         r" \(UTC([+-]\d{1,2})?(?::(\d{2}))?\)\s*</timestamp>")


def cursor_time(text):
    """The ISO time of a Cursor user record's <timestamp>, or None."""
    found = CURSOR_TIME.search(text)
    if not found:
        return None
    from datetime import timedelta, timezone
    hours = int(found.group(2) or 0)
    minutes = int(found.group(3) or 0) * (1 if hours >= 0 else -1)
    try:
        at = datetime.strptime(found.group(1), "%b %d, %Y, %I:%M %p")
    except ValueError:
        return None
    return at.replace(tzinfo=timezone(timedelta(hours=hours, minutes=minutes))).isoformat()


class CursorConversationCapture(CodexConversationCapture):
    """Cursor (the CLI agent; the editor's agent writes the same files): one JSONL per chat
    under ~/.cursor/projects/<project>/agent-transcripts/. Its hooks for the prompt and the reply
    do not run in the CLI, so the transcript is read. A user record wraps what was typed in
    <user_query>; only that is kept, and only the text blocks of a reply."""

    CLIENT = "cursor"

    def _files(self):
        return sorted(self.sessions_root.glob("*/agent-transcripts/**/*.jsonl"))

    def _session_meta(self, path):
        self._said_at = None                     # replies are dated by the message they answer
        return path.stem, None, "global"

    def _forget_call(self, item):
        if item.get("role") != "assistant":
            return False
        return any(isinstance(block, dict) and block.get("type") == "tool_use" and (
            str(block.get("name", "")).endswith(self.FORGET_TOOL)
            or str((block.get("input") or {}).get("toolName", "")).endswith(self.FORGET_TOOL))
            for block in (item.get("message") or {}).get("content") or [])

    def _message(self, item):
        role = item.get("role")
        content = (item.get("message") or {}).get("content")
        if role not in {"user", "assistant"} or not isinstance(content, list):
            return None
        text = "\n".join(block.get("text", "") for block in content
                         if isinstance(block, dict) and block.get("type") == "text")
        if role == "user":
            self._said_at = cursor_time(text) or getattr(self, "_said_at", None)
            query = CURSOR_QUERY.search(text)
            text = query.group(1) if query else re.sub(r"<timestamp>.*?</timestamp>", "", text)
        if getattr(self, "_said_at", None):
            item["timestamp"] = self._said_at     # read by scan() as when it was said
        return role, text


def cursor_root():
    return Path.home() / ".cursor" / "projects"


def copilot_root():
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "session-state"


class HookConversationCapture(OpenCodeConversationCapture):
    """Gemini CLI, Cursor, Copilot CLI: Graph-MIND's hooks (hook.py) write the same records,
    one folder per agent under ~/.graph-mind/hooks, stored under that agent's name."""

    def __init__(self, client, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.CLIENT = client


def opencode_root():
    from graph_mind.paths import graph_mind_home
    return graph_mind_home() / "opencode"


def cowork_root():
    """Where Claude desktop keeps Cowork sessions: the Store (MSIX) build or the plain installer."""
    candidates = [Path(os.environ.get("APPDATA", "")) / "Claude" / "local-agent-mode-sessions"]
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    if packages.is_dir():
        candidates += [package / "LocalCache" / "Roaming" / "Claude" / "local-agent-mode-sessions"
                       for package in packages.glob("Claude_*")]
    return next((path for path in candidates if path.is_dir()), None)

