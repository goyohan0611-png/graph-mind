"""Persistent background capture for Graph-MIND code and conversation memory."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from graph_mind.associative_memory import AssociativeMemoryIndex
from graph_mind.coding_memory import CodingMemoryCapture, CodingMemoryStore
from graph_mind.conversation_memory import (
    ClaudeCodeConversationCapture,
    ClaudeCoworkConversationCapture,
    CodexConversationCapture,
    ConversationMemoryStore,
    cowork_root,
)
from graph_mind.paths import default_coding_state_dir, default_development_db, graph_mind_home
from graph_mind.semantic_memory import SemanticMemoryIngestion

SERVICE_VERSION = "automatic-capture-v0.2"


def default_config_path():
    return graph_mind_home() / "automatic-capture.json"


def default_service_state_dir():
    return graph_mind_home() / "automatic-capture"


def running_service(state_dir=None):
    """The live capture service's pid, or None. A pid file left by a crash, or naming a pid the
    system has since given to another program, is not a running service."""
    import psutil
    try:
        path = Path(state_dir or default_service_state_dir()) / "service.pid"
        pid = int(path.read_text(encoding="ascii"))
        mine = (os.getpid(), os.getppid())      # a venv's pythonw runs the service as its child
        if pid not in mine and "capture_cli" in " ".join(psutil.Process(pid).cmdline()):
            return pid
    except (OSError, ValueError, psutil.Error):
        pass
    return None


def ensure_service() -> bool:
    """Start the capture service unless it runs; True when it was started.

    It starts at login, and nothing else brought it back: a crash, a Task Manager kill or an
    update closing it meant no conversation was saved until the next login. Now every memory
    server of an app that relies on it checks it now and then and starts it again."""
    import fasteners
    state = default_service_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(state / "start.lock")):    # two apps, one service
        if running_service(state):
            return False
        python = Path(sys.executable)
        if sys.platform == "win32":
            python = python.with_name("pythonw.exe")                 # no console window
            detach = {"creationflags": subprocess.DETACHED_PROCESS}
        else:
            detach = {"start_new_session": True}
        child = subprocess.Popen([str(python), "-m", "graph_mind.capture_cli", "run"],
                                 cwd=Path.home(), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
        (state / "service.pid").write_text(f"{child.pid}\n", encoding="ascii")
        return True


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
        # conversation held on this PC can be recalled on any other (sharing.py).
        from graph_mind.sharing import folder
        # config, GRAPH_MIND_FOLDER, or the one chosen in conversation; the service's own wins
        shared = folder()
        if self.config.get("memory_folder"):
            from graph_mind.sharing import is_database
            chosen = self.config["memory_folder"]
            shared = chosen if is_database(chosen) else Path(chosen).expanduser()
        publish = None
        if shared:
            from graph_mind.sharing import append

            def publish(turn):                  # to a folder or a Postgres URL
                append(shared, "turn", turn)
        from graph_mind.forget import forget_ids

        def forget(turn_ids):
            forget_ids({"turn_ids": turn_ids}, self.db_path, shared)
        hooks = {"workspace_scopes": scopes, "now": self.now, "publish": publish,
                 "forget": forget}
        sources = []
        # A source whose folder does not exist yet (Codex never run on this PC) is skipped, not
        # fatal: it used to stop every source, so a Claude-Code-only user captured nothing.
        if Path(self.config["codex_sessions_root"]).is_dir():
            sources.append(("codex", CodexConversationCapture(
                self.config["codex_sessions_root"], store, state_path=self.conversation_cursor,
                **hooks), self.conversation_cursor))
        claude_root = Path(self.config.get("claude_projects_root")
                           or Path.home() / ".claude" / "projects")
        if claude_root.is_dir():
            sources.append(("claude-code", ClaudeCodeConversationCapture(
                claude_root, store, state_path=self.claude_cursor, **hooks), self.claude_cursor))
        cowork = self.config.get("claude_cowork_root") or cowork_root()
        if cowork and Path(cowork).is_dir():
            sources.append(("claude-cowork", ClaudeCoworkConversationCapture(
                cowork, store, state_path=self.cowork_cursor, **hooks), self.cowork_cursor))
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

    def run_once(self, *, capture_code=True):
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
                  "associative": associative,
                  "errors": errors,
                  "warnings": self._stall_warnings(conversation)}
        self._write_status(result)
        return result

    STALL_ROWS = 2000

    def _stall_warnings(self, conversation):
        """A source whose transcripts keep growing while no conversation comes out of them: the
        app most likely changed its transcript format, and capture would otherwise stop without
        a word. Counted across passes in capture-health.json; reset by the next turn found."""
        path = self.state_dir / "capture-health.json"
        try:
            health = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = {}
        warnings = []
        for name, outcome in conversation.items():
            if not isinstance(outcome, dict) or "scanned_rows" not in outcome:
                continue
            entry = health.setdefault(name, {"rows_without_turns": 0, "last_turn_at": None})
            if outcome.get("recorded_turns"):
                entry.update(rows_without_turns=0, last_turn_at=self.now())
            else:
                entry["rows_without_turns"] += outcome["scanned_rows"]
            if entry["rows_without_turns"] >= self.STALL_ROWS:
                warnings.append(
                    f"{name}: {entry['rows_without_turns']} new transcript lines and no "
                    f"conversation found in them since {entry['last_turn_at'] or 'capture began'}"
                    " - the app may have changed its format; update Graph-MIND or report it")
        _atomic_json(path, health)
        return warnings

    def _write_status(self, result):
        _atomic_json(self.status_path, {**result, "pid": os.getpid(),
                                       "config_db": str(self.db_path)})

    def embed_new_turns(self, limit=512):
        """Make what was just captured searchable by meaning before anyone asks about it (the
        memory servers pick the vectors up from the shared cache). Only the long-running service
        does this: a one-off `once` or a test should not load the embedding model."""
        try:
            from graph_mind.local_embedder import default_embedder
            from graph_mind.warmup import warm_turns
            if getattr(self, "_embedder", None) is None:
                self._embedder = default_embedder(self.db_path)
            return warm_turns(self.db_path, limit=limit, embedder=self._embedder)
        except Exception:                       # capture must go on if embedding cannot
            return 0

    def lend_model(self):
        """Load the model now and let the memory servers use it (local_embedder.serve), so an
        app's search by meaning works from its first question without loading its own."""
        try:
            from graph_mind.local_embedder import default_embedder, serve
            if getattr(self, "_embedder", None) is None:
                self._embedder = default_embedder(self.db_path)
            self._embedder.embed(["warm up"])
            serve(self._embedder)
        except Exception:                       # no model on disk: the servers load their own
            pass

    def run_forever(self, *, max_iterations=None):
        if running_service(self.state_dir):
            return                              # one service per PC (login item + a restart)
        self.pid_path.write_text(str(os.getpid()) + "\n", encoding="ascii")
        if max_iterations is None:
            self.lend_model()
        iteration = 0
        last_code_scan = None
        try:
            while max_iterations is None or iteration < max_iterations:
                monotonic_now = time.monotonic()
                code_due = (last_code_scan is None or monotonic_now - last_code_scan
                            >= self.config.get("code_poll_seconds", 120))
                self.run_once(capture_code=code_due)
                self.embed_new_turns()
                if code_due:
                    last_code_scan = time.monotonic()
                iteration += 1
                if max_iterations is None or iteration < max_iterations:
                    time.sleep(self.config["poll_seconds"])
        finally:
            try:
                if self.pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self.pid_path.unlink()
            except (FileNotFoundError, OSError):
                pass

