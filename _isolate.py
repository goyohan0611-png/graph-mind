"""Imported first by every test module: Graph-MIND's per-user home (config, stores, Postgres hub)
becomes an empty temporary folder, so a test run never reads or writes a developer's own memory.

Once a developer's PC joined a shared hub, the tests' saves went into it and its 4,600 real lines
came back into the tests' answers: config.json under the real home was read by every test.
"""
import os
import tempfile

os.environ["GRAPH_MIND_HOME"] = tempfile.mkdtemp(prefix="graph-mind-test-home-")
for _name in ("GRAPH_MIND_CONFIG", "GRAPH_MIND_FOLDER", "GRAPH_MIND_DB"):
    os.environ.pop(_name, None)
