"""Stable user-owned paths shared by every Graph-MIND client."""
from pathlib import Path
import hashlib
import os


def graph_mind_home():
    configured = os.environ.get("GRAPH_MIND_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".graph-mind"


def default_development_db():
    return graph_mind_home() / "development.sqlite"


def default_capture_state_dir(project_id):
    suffix = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    return graph_mind_home() / "capture" / suffix


def default_coding_state_dir(project_id):
    suffix = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    return graph_mind_home() / "coding" / suffix


def use_wal(db):
    """Put a store in WAL mode, waiting for the other clients if need be. Changing the journal
    mode needs the database to itself, and SQLite does not wait for that the way it waits for a
    write: three clients opening a new store at once failed with "database is locked" (macOS CI).
    """
    import sqlite3
    import time
    for attempt in range(100):
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or attempt == 99:
                raise
            time.sleep(0.1)
