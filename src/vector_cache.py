"""Binary vector cache: float32 rows in one file, keys in a small index.

The JSON cache this replaces held every dimension as a Python float object — a 1.1GB file became
3-4GB of RAM and took minutes to parse, which is what got two evaluation runs killed for memory
pressure. Same vectors as float32 are ~8x smaller and load by reading bytes.

Layout:  <dir>/vectors-<id>.f32  rows of `dim` little-endian floats, appended in insertion order
         <dir>/index.json       {"dim", "model", "keys": {sha256: row}, "blob": "vectors-<id>.f32"}

Several processes (Claude's and Codex's memory servers) share one cache. Each save writes a NEW
blob, then swaps index.json in one atomic rename, so a reader always gets an index together with
the blob it was written for. Rewriting vectors.f32 in place let a reader pair one process's index
with another's rows: every vector silently wrong. Older caches without "blob" read vectors.f32.

    python vector_cache.py            # self-check
    python vector_cache.py old.json   # convert a legacy JSON cache next to it
"""
from __future__ import annotations

from array import array
from pathlib import Path
import json
import os
import sys
import time


class VectorCache:
    def __init__(self, directory, dim: int | None = None, model: str = ""):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        meta, rows, self._stamp = self._read()
        self.blob_path = self.dir / meta.get("blob", "vectors.f32")
        self.dim = meta.get("dim") or dim
        self.model = meta.get("model") or model
        self.keys: dict[str, int] = meta.get("keys", {})
        self._rows = rows
        self._dirty = 0

    def _read(self):
        """(index, rows, index mtime) as saved on disk; empty when nothing is saved yet."""
        for _ in range(3):                 # a save between reading the index and its blob
            try:
                stamp = self.index_path.stat().st_mtime_ns
                meta = json.loads(self.index_path.read_text(encoding="utf-8"))
                rows = array("f")
                blob = self.dir / meta.get("blob", "vectors.f32")
                if blob.exists() and meta.get("dim"):
                    rows.frombytes(blob.read_bytes())
                return meta, rows, stamp
            except FileNotFoundError:
                if not self.index_path.exists():
                    return {}, array("f"), None
            except (OSError, ValueError):
                time.sleep(0.05)
        return {}, array("f"), None

    def refresh(self) -> int:
        """Take in the vectors other processes saved since this one last looked. The capture
        service embeds new turns as it stores them; a memory server that started earlier sees
        them here instead of embedding them all again. Cheap when nothing changed (one stat)."""
        try:
            if self.index_path.stat().st_mtime_ns == self._stamp:
                return 0
        except OSError:
            return 0
        meta, rows, stamp = self._read()
        dim, added = meta.get("dim"), 0
        if dim and (self.dim is None or dim == self.dim):
            self.dim = self.dim or dim
            for key, row in meta.get("keys", {}).items():
                if key not in self.keys:
                    self.keys[key] = len(self._rows) // dim
                    self._rows.extend(rows[row * dim:(row + 1) * dim])
                    added += 1
        self._stamp = stamp
        return added

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def __len__(self) -> int:
        return len(self.keys)

    def get(self, key: str):
        row = self.keys.get(key)
        if row is None:
            return None
        start = row * self.dim
        return self._rows[start:start + self.dim]

    def put(self, key: str, vector) -> None:
        if key in self.keys:
            return
        vector = array("f", vector)
        if self.dim is None:
            self.dim = len(vector)
        if len(vector) != self.dim:
            raise ValueError(f"expected {self.dim} dims, got {len(vector)}")
        self.keys[key] = len(self._rows) // self.dim
        self._rows.extend(vector)
        self._dirty += 1

    def flush(self) -> None:
        if not self._dirty and self.blob_path.exists():
            return
        import fasteners
        # One saver at a time, and each merges what is on disk first: otherwise the last writer
        # wins and the vectors only the other process had are dropped and embedded again.
        with fasteners.InterProcessLock(str(self.dir / "save.lock")):
            self.refresh()
            blob = self.dir / f"vectors-{os.getpid()}-{time.time_ns()}.f32"
            blob.write_bytes(self._rows.tobytes())
            staged = self.dir / f"index.{os.getpid()}.tmp"
            staged.write_text(json.dumps({"dim": self.dim, "model": self.model, "keys": self.keys,
                                          "blob": blob.name}), encoding="utf-8")
            os.replace(staged, self.index_path)
            self._stamp = self.index_path.stat().st_mtime_ns
        self.blob_path = blob
        for old in self.dir.glob("vectors*.f32"):   # blobs no index points at any more
            if old != blob:
                try:
                    old.unlink()
                except OSError:                     # another process is reading it right now
                    pass
        self._dirty = 0


def convert(json_path, directory=None, model: str = "") -> VectorCache:
    """One-time migration of a legacy {key: [floats]} JSON cache."""
    json_path = Path(json_path)
    cache = VectorCache(directory or json_path.with_suffix(".vec"), model=model)
    for key, vector in json.loads(json_path.read_text(encoding="utf-8")).items():
        cache.put(key, vector)
    cache.flush()
    return cache


def _self_check():
    import tempfile
    directory = Path(tempfile.mkdtemp()) / "vec"
    cache = VectorCache(directory, model="test")
    cache.put("a", [0.5, -0.25, 1.0])
    cache.put("b", [1.0, 0.0, 0.0])
    cache.put("a", [9, 9, 9])                      # duplicate key keeps the first vector
    assert len(cache) == 2 and "a" in cache and "zz" not in cache
    assert list(cache.get("a")) == [0.5, -0.25, 1.0]
    assert cache.get("zz") is None
    cache.flush()

    reopened = VectorCache(directory)
    assert len(reopened) == 2 and reopened.dim == 3 and reopened.model == "test"
    assert list(reopened.get("b")) == [1.0, 0.0, 0.0]
    reopened.put("c", [2.0, 2.0, 2.0])
    reopened.flush()
    assert list(VectorCache(directory).get("c")) == [2.0, 2.0, 2.0]

    # two processes, same cache, different new vectors, saving in turn: whichever index a reader
    # gets, every key it lists must come back with ITS vector, never the other writer's row
    first, second = VectorCache(directory), VectorCache(directory)
    first.put("only-first", [7.0, 7.0, 7.0])
    second.put("only-second", [8.0, 8.0, 8.0])
    second.put("also-second", [6.0, 6.0, 6.0])
    first.flush()
    second.flush()
    after = VectorCache(directory)
    assert list(after.get("only-first")) == [7.0, 7.0, 7.0], "the second save kept the first's"
    assert list(after.get("only-second")) == [8.0, 8.0, 8.0]
    assert list(after.get("also-second")) == [6.0, 6.0, 6.0]
    assert list(after.get("c")) == [2.0, 2.0, 2.0]
    assert len(list(directory.glob("vectors*.f32"))) == 1, "stale blobs are removed"

    # a reader that loaded before another process saved picks the new vectors up on refresh
    reader, writer = VectorCache(directory), VectorCache(directory)
    writer.put("late", [3.0, 1.0, 4.0])
    writer.flush()
    assert "late" not in reader and reader.refresh() == 1
    assert list(reader.get("late")) == [3.0, 1.0, 4.0] and reader.refresh() == 0
    assert list(reader.get("only-first")) == [7.0, 7.0, 7.0]

    legacy = directory.parent / "legacy.json"
    legacy.write_text(json.dumps({"k1": [1.0, 2.0, 3.0], "k2": [4.0, 5.0, 6.0]}), encoding="utf-8")
    migrated = convert(legacy)
    assert len(migrated) == 2 and list(migrated.get("k2")) == [4.0, 5.0, 6.0]
    print("vector_cache self-check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cache = convert(sys.argv[1])
        print(f"converted {len(cache)} vectors -> {cache.dir}")
    else:
        _self_check()
