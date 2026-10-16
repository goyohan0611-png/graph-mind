"""Several processes saving one vector cache at once (the capture service and every app's memory
server do) must never pair a key with another text's vector. Before the fix, on Linux, a process
could load an index whose blob another process had just removed, and its next save wrote every
listed key against the wrong rows: the bench's cached vectors had cosine ~0.07 with the model's.

    python test_vector_cache_race.py [path/to/vector_cache.py | git:<rev>]   # another version
"""
import importlib.util
from array import array
import multiprocessing
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest

MODULE = Path(__file__).resolve().parent.parent / "src/graph_mind/vector_cache.py"


def load(path):
    spec = importlib.util.spec_from_file_location("vector_cache_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vector(key):
    rng = random.Random(key)
    return list(array("f", [rng.random() for _ in range(8)]))


def writer(path, directory, name, rounds):
    cache_class = load(path).VectorCache
    for i in range(rounds):
        cache = cache_class(directory)              # a server starting, or reloading
        for j in range(5):
            cache.put(f"{name}-{i}-{j}", vector(f"{name}-{i}-{j}"))
        # a question's refresh() while a background thread embeds into the same cache
        side = threading.Thread(target=lambda: [cache.put(f"{name}-{i}-t{k}",
                                                          vector(f"{name}-{i}-t{k}"))
                                                for k in range(20)])
        side.start()
        for _ in range(5):
            cache.refresh()
        side.join()
        cache.flush()


def wrong_vectors(path=MODULE, processes=4, rounds=40):
    directory = tempfile.mkdtemp()
    workers = [multiprocessing.Process(target=writer, args=(str(path), directory, f"w{n}", rounds))
               for n in range(processes)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    cache = load(path).VectorCache(directory)
    wrong = sum(1 for key in cache.keys if list(cache.get(key)) != vector(key))
    return wrong, len(cache.keys), [w.exitcode for w in workers]


class VectorCacheRaceTests(unittest.TestCase):
    def test_concurrent_saves_never_mix_up_vectors(self):
        wrong, keys, exits = wrong_vectors()
        self.assertEqual(exits, [0, 0, 0, 0])            # no save crashed (Windows: index open)
        self.assertEqual(wrong, 0)
        self.assertGreater(keys, 0)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if target.startswith("git:"):
            import subprocess
            source = subprocess.run(["git", "show", target[4:] + ":src/graph_mind/vector_cache.py"],
                                    capture_output=True, text=True, check=True,
                                    cwd=MODULE.parent).stdout
            target = Path(tempfile.mkdtemp()) / "vector_cache.py"
            target.write_text(source, encoding="utf-8")
        for _ in range(3):
            print(sys.argv[1], "wrong %d of %d, exits %s" % wrong_vectors(Path(target)), flush=True)
    else:
        unittest.main()
