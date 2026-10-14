"""Graph-MIND: local memory for AI assistants, shared over MCP.

Capture saves every conversation on this PC without calling a model; recall hands the model
the few thousand tokens of the past that answer its question.
"""
from pathlib import Path
import re


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
