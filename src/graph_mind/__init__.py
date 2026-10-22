"""Graph-MIND: local memory for AI assistants, shared over MCP.

Capture saves every conversation on this PC without calling a model; recall hands the model
the few thousand tokens of the past that answer its question.
"""
import os
from pathlib import Path
import re

# Before numpy is imported anywhere: OpenBLAS reserves a buffer per core when it loads, 233 MB
# of memory on an 8-core PC, in every memory server an app starts. One thread is plenty for the
# one query-against-pieces product per question this package asks of it.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")


def _version() -> str:
    """The checkout's pyproject.toml, else the installed distribution's version: a hand-kept
    constant once drifted to 0.5.0 at release 0.3.1."""
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = project.read_text(encoding="utf-8") if project.exists() else ""
    if 'name = "graph-mind-memory"' in text:
        found = re.search(r'^version = "([^"]+)"', text, re.M)
        if found:
            return found.group(1)
    try:
        from importlib.metadata import version
        return version("graph-mind-memory")
    except Exception:
        return "0+unknown"


__version__ = _version()
