"""Persistent background capture for Graph-MIND code and conversation memory."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import time

from associative_memory import AssociativeMemoryIndex
from coding_memory import CodingMemoryCapture, CodingMemoryStore
from conversation_memory import (ClaudeCodeConversationCapture, ClaudeCoworkConversationCapture,
                                 cowork_root, CodexConversationCapture,
                                 ConversationMemoryStore)
from development_paths import default_coding_state_dir, default_development_db, graph_mind_home
from semantic_memory import SemanticMemoryIngestion
from semantic_llm_extractor import (OpenAIResponsesSemanticExtractor,
                                    SemanticLLMIngestion)


SERVICE_VERSION = "automatic-capture-v0.2"


def default_config_path():
    return graph_mind_home() / "automatic-capture.json"


def default_service_state_dir():
    return graph_mind_home() / "automatic-capture"


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _workspace_key(path):
    return str(Path(path).expanduser().resolve())


def make_config(*, workspace, project_id, session_id, sessions_root,
                poll_seconds=5, code_poll_seconds=120, db_path=None):
    workspace = _workspace_key(workspace)
    sessions_root = _workspace_key(sessions_root)
    return {
        "service_version": SERVICE_VERSION,
        "db_path": str(Path(db_path or default_development_db()).expanduser().resolve()),
        "poll_seconds": max(1, int(poll_seconds)),
        "code_poll_seconds": max(int(poll_seconds), int(code_poll_seconds)),
        "semantic_llm": {"enabled": False, "provider": "openai",
                         "model": None, "poll_seconds": 60, "limit": 25},
        "codex_sessions_root": sessions_root,
        "workspace_scopes": {workspace: project_id},
        "code_workspaces": [{"workspace": workspace, "project_id": project_id,
                             "session_id": session_id,
                             "state_dir": str(default_coding_state_dir(project_id))}],
    }


def load_config(path=None):
    path = Path(path or default_config_path()).expanduser().resolve()
    if not path.is_file():
        raise ValueError("AUTOMATIC_CAPTURE_CONFIG_REQUIRED")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("service_version") != SERVICE_VERSION:
        raise ValueError("AUTOMATIC_CAPTURE_CONFIG_VERSION_MISMATCH")
    if not isinstance(value.get("code_workspaces"), list):
        raise ValueError("AUTOMATIC_CAPTURE_WORKSPACES_REQUIRED")
    return value


class AutomaticCaptureService:
    def __init__(self, config, *, state_dir=None, now=None):
        self.config = config
        self.state_dir = Path(state_dir or default_service_state_dir()).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.state_dir / "status.json"
        self.pid_path = self.state_dir / "service.pid"
        self.conversation_cursor = self.state_dir / "codex-session-cursor.json"
        self.claude_cursor = self.state_dir / "claude-code-session-cursor.json"
        self.cowork_cursor = self.state_dir / "claude-cowork-session-cursor.json"
        self.now = now or _now
        self.db_path = Path(config["db_path"]).expanduser().resolve()

    def _coding_capture(self, entry, store):
        return CodingMemoryCapture(entry["workspace"], store,
            project_id=entry["project_id"], session_id=entry["session_id"],
            state_dir=entry.get("state_dir") or default_coding_state_dir(
                entry["project_id"]), now=self.now)

    def _conversation_captures(self, store):
        """Every conversation source on this machine, each with its own read cursor.

        Codex is configured explicitly. Claude Code is picked up from ~/.claude/projects when
        that folder exists, unless claude_projects_root names another one, so that whatever the
        user talks to is stored in full and not only what a model chose to remember.
        """
        scopes = self.config.get("workspace_scopes", {})
        # With a shared folder, every captured turn also goes into the shared log, so a
        # conversation held on this PC can be recalled on any other (brain_log.py).
        from brain_log import folder
        shared = folder()                       # config, GRAPH_MIND_FOLDER, or the chosen one
        if self.config.get("memory_folder"):    # the service's own config wins
            from brain_log import is_database
            chosen = self.config["memory_folder"]
            shared = chosen if is_database(chosen) else Path(chosen).expanduser()     # folder() also reads the
                                                                   # one chosen in conversation
        publish = None
        if shared:
            from brain_log import append
            publish = lambda turn: append(shared, "turn", turn)     # a folder or a Postgres URL
        sources = []
        # A source whose folder does not exist yet (Codex never run on this PC) is skipped, not
        # fatal: it used to stop every source, so a Claude-Code-only user captured nothing.
        if Path(self.config["codex_sessions_root"]).is_dir():
            sources.append(("codex", CodexConversationCapture(
                self.config["codex_sessions_root"], store, state_path=self.conversation_cursor,
                workspace_scopes=scopes, now=self.now, publish=publish), self.conversation_cursor))
        claude_root = Path(self.config.get("claude_projects_root")
                           or Path.home() / ".claude" / "projects")
        if claude_root.is_dir():
            sources.append(("claude-code", ClaudeCodeConversationCapture(
                claude_root, store, state_path=self.claude_cursor,
                workspace_scopes=scopes, now=self.now, publish=publish), self.claude_cursor))
        cowork = self.config.get("claude_cowork_root") or cowork_root()
        if cowork and Path(cowork).is_dir():
            sources.append(("claude-cowork", ClaudeCoworkConversationCapture(
                cowork, store, state_path=self.cowork_cursor,
                workspace_scopes=scopes, now=self.now, publish=publish), self.cowork_cursor))
        return sources

    def baseline(self):
        coding = []
        with CodingMemoryStore(self.db_path) as store:
            for entry in self.config["code_workspaces"]:
                capture = self._coding_capture(entry, store)
                if capture.manifest_path.exists():
                    coding.append({"project_id": entry["project_id"],
                                   "status": "BASELINE_ALREADY_EXISTS",
                                   "manifest": str(capture.manifest_path)})
                else:
                    coding.append({"project_id": entry["project_id"],
                                   **capture.initialize_baseline()})
        with ConversationMemoryStore(self.db_path) as store:
            conversation = {
                name: ({"status": "BASELINE_ALREADY_EXISTS", "state_path": str(cursor)}
                       if cursor.exists() else capture.initialize_baseline())
                for name, capture, cursor in self._conversation_captures(store)}
        result = {"status": "BASELINE_READY", "service_version": SERVICE_VERSION,
                  "at": self.now(), "coding": coding,
                  "conversation": conversation.get("codex"), "conversation_sources": conversation}
        self._write_status(result)
        return result

    def run_once(self, *, capture_code=True, capture_llm=True):
        started = self.now()
        coding, errors = [], []
        if capture_code:
            with CodingMemoryStore(self.db_path) as store:
                for entry in self.config["code_workspaces"]:
                    try:
                        capture = self._coding_capture(entry, store)
                        if not capture.manifest_path.exists():
                            outcome = capture.initialize_baseline()
                        else:
                            outcome = capture.scan(
                                reason="Automatically observed workspace change; intent pending",
                                related_tests=[])
                        coding.append({"project_id": entry["project_id"], **outcome})
                    except Exception as error:  # keep other capture sources alive
                        errors.append({"source": "code",
                            "project_id": entry.get("project_id"),
                            "error": type(error).__name__, "detail": str(error)})
        else:
            coding.append({"status": "SKIPPED_NOT_DUE"})
        semantic = None
        conversation = {}
        try:
            with ConversationMemoryStore(self.db_path) as store:
                for name, capture, cursor in self._conversation_captures(store):
                    try:                       # one broken source must not stop the others
                        conversation[name] = (capture.scan() if cursor.exists()
                                              else capture.initialize_baseline())
                    except Exception as error:
                        errors.append({"source": f"conversation:{name}",
                                       "error": type(error).__name__, "detail": str(error)})
        except Exception as error:
            errors.append({"source": "conversation", "error": type(error).__name__,
                           "detail": str(error)})
        try:
            with SemanticMemoryIngestion(self.db_path, now=self.now) as ingestion:
                semantic = ingestion.process_new(limit=200)
        except Exception as error:
            errors.append({"source": "semantic", "error": type(error).__name__,
                           "detail": str(error)})
        semantic_llm = {"status": "DISABLED"}
        llm_config = self.config.get("semantic_llm", {})
        if llm_config.get("enabled") and not capture_llm:
            semantic_llm = {"status": "SKIPPED_NOT_DUE"}
        elif llm_config.get("enabled"):
            try:
                if llm_config.get("provider") != "openai":
                    raise ValueError("UNSUPPORTED_SEMANTIC_LLM_PROVIDER")
                extractor = OpenAIResponsesSemanticExtractor(llm_config.get("model"))
                with SemanticLLMIngestion(self.db_path, now=self.now) as ingestion:
                    semantic_llm = ingestion.process_new(
                        extractor, limit=int(llm_config.get("limit", 25)))
            except Exception as error:
                errors.append({"source": "semantic_llm",
                               "error": type(error).__name__, "detail": str(error)})
                semantic_llm = {"status": "ERROR"}
        associative = None
        try:
            with AssociativeMemoryIndex(self.db_path) as index:
                associative = index.sync_sources(limit=1000)
        except Exception as error:
            errors.append({"source": "associative", "error": type(error).__name__,
                           "detail": str(error)})
        result = {"status": "CAPTURE_ERRORS" if errors else "CAPTURE_OK",
                  "service_version": SERVICE_VERSION, "started_at": started,
                  "completed_at": self.now(), "coding": coding,
                  "conversation": conversation.get("codex"),
                  "conversation_sources": conversation, "semantic": semantic,
                  "semantic_llm": semantic_llm,
                  "associative": associative,
                  "errors": errors}
        self._write_status(result)
        return result

    def _write_status(self, result):
        _atomic_json(self.status_path, {**result, "pid": os.getpid(),
                                       "config_db": str(self.db_path)})

    def embed_new_turns(self, limit=512):
        """Make what was just captured searchable by meaning before anyone asks about it (the
        memory servers pick the vectors up from the shared cache). Only the long-running service
        does this: a one-off `once` or a test should not load the embedding model."""
        try:
            from embedding_warmup import warm_turns
            from semantic_recall import default_embedder
            if getattr(self, "_embedder", None) is None:
                self._embedder = default_embedder(self.db_path)
            return warm_turns(self.db_path, limit=limit, embedder=self._embedder)
        except Exception:                       # capture must go on if embedding cannot
            return 0

    def run_forever(self, *, max_iterations=None):
        self.pid_path.write_text(str(os.getpid()) + "\n", encoding="ascii")
        iteration = 0
        last_code_scan = None
        last_llm_scan = None
        try:
            while max_iterations is None or iteration < max_iterations:
                monotonic_now = time.monotonic()
                code_due = (last_code_scan is None or monotonic_now - last_code_scan
                            >= self.config.get("code_poll_seconds", 120))
                llm_config = self.config.get("semantic_llm", {})
                llm_due = (last_llm_scan is None or monotonic_now - last_llm_scan
                           >= llm_config.get("poll_seconds", 60))
                self.run_once(capture_code=code_due, capture_llm=llm_due)
                self.embed_new_turns()
                if code_due:
                    last_code_scan = time.monotonic()
                if llm_config.get("enabled") and llm_due:
                    last_llm_scan = time.monotonic()
                iteration += 1
                if max_iterations is None or iteration < max_iterations:
                    time.sleep(self.config["poll_seconds"])
        finally:
            try:
                if self.pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self.pid_path.unlink()
            except (FileNotFoundError, OSError):
                pass


def config_identity(config):
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
