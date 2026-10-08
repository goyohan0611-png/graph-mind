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
