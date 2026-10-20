"""The on-device embedder: multilingual mpnet (int8), mean pooling, vectors cached on disk.

No text leaves the machine. Split out of the v0.7.3 research script (semantic_grounding_v073.py),
which still imports it from here, so the product no longer pulls that script's experiment chain.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import math
import os
import threading
import time

from graph_mind.vector_cache import VectorCache, convert

# "repo@file": the int8 export in the model's own repo. Against MiniLM (0.9.9 and before), on
# research/runs/graph-v9.13-int8-models: Korean questions 19/20 vs 17/20, formal Korean 17 vs 13,
# English evidence R@5 99.0% vs 97.9%, 699 MB vs 869 MB loaded; 2.4x slower to embed, 10 ms a query
LOCAL_MODEL = ("sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
               "@onnx/model_quint8_avx2.onnx")
OLD_MODELS = ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",)


def own_hf_home() -> Path:
    from graph_mind.paths import graph_mind_home
    return graph_mind_home() / "huggingface"


def use_own_hf_home() -> None:
    """Hugging Face keeps everything under HF_HOME (~/.cache/huggingface): the model, its xet
    download cache, its lock files. On one fresh Windows profile that folder could not be created
    (access denied), so the installer moved all of it under ~/.graph-mind; from then on every
    process must look there. Read before huggingface_hub is imported, which fixes the paths."""
    if not os.environ.get("HF_HOME") and own_hf_home().is_dir():
        os.environ["HF_HOME"] = str(own_hf_home())


use_own_hf_home()


MODEL_FILES = ("tokenizer.json", "onnx/model.onnx")


def model_files(model=LOCAL_MODEL) -> tuple:
    """(repo, (tokenizer file, ONNX file)) for "repo" or "repo@onnx/another.onnx"."""
    repo, _, onnx_file = model.partition("@")
    return repo, (MODEL_FILES[0], onnx_file or MODEL_FILES[1])


def model_on_disk() -> bool:
    """Both files the embedder reads are already downloaded (an install from before 0.4 has the
    torch weights in the same folder, but not the ONNX file)."""
    hf_home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    repo, files = model_files()
    snapshots = hf_home / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots"
    return any(all((snapshot / name).exists() for name in files)
               for snapshot in (snapshots.iterdir() if snapshots.is_dir() else []))


def model_paths(model=LOCAL_MODEL) -> tuple:
    """Local paths of the tokenizer and the ONNX model, downloaded on first use."""
    from huggingface_hub import hf_hub_download
    repo, files = model_files(model)
    return tuple(hf_hub_download(repo, name) for name in files)


class LocalEmbedder:
    """On-device multilingual embeddings: the model's own ONNX export run by ONNX Runtime,
    mean pooling, L2-normalized. No text leaves the machine.

    Until 0.4 this ran on torch + transformers: same model, same vectors (on 1,000 texts the
    largest difference was 2.4e-7, cosine 1.000000), but a 2 GB install, the Visual C++ runtime,
    and 48-71 s to load on a OneDrive-synced Windows PC against 6-9 s now."""

    _model = None            # an onnxruntime.InferenceSession
    _tok = None              # a tokenizers.Tokenizer
    _lock = threading.RLock()  # model load + cache writes are not thread-safe

    FLUSH_EVERY = 5000   # vectors between writes: bounds lost work without rewriting per call
    # Seconds a question may wait for the model to load before it is answered by word search
    # alone. None waits (benchmarks, tests, the capture service); the MCP server sets 0.5: loading
    # torch took 38.8 s for an app's first question on a OneDrive-synced Windows PC; now 2.4 s.
    WAIT_FOR_MODEL = None
    # Ask the capture service's model first (serve below). The MCP server sets it: every app
    # starts its own server, and each one loading the model cost 7-17 s before search by meaning
    # worked and ~500 MB of memory per app, while the capture service has it loaded all day.
    USE_SERVICE = False
    # Rest between backlog chunks (_encode_paced). The capture service and the MCP server set
    # it; benchmarks and tests run at full speed.
    PACE = False
    CHUNK = 32
    _backlog = threading.Lock()
    _loaded = threading.Event()
    _loading = threading.Lock()

    def __init__(self, cache_path, model=LOCAL_MODEL):
        # float32 rows in a sibling directory: the old JSON cache turned 1.1GB on disk into 3-4GB
        # of Python float objects and got two evaluation runs killed for memory pressure.
        self.cache_path = Path(cache_path)
        self.model_name = model
        self.cache = VectorCache(self.cache_path.with_suffix(".vec"), model=model)
        if not len(self.cache) and self.cache_path.exists():
            self.cache = convert(self.cache_path, self.cache_path.with_suffix(".vec"), model)
        self._since = 0

    def _load(self):
        if LocalEmbedder._model is None:
            from tokenizers import Tokenizer
            import onnxruntime
            tokenizer_file, model_file = model_paths(self.model_name)
            tokenizer = Tokenizer.from_file(tokenizer_file)
            tokenizer.enable_truncation(256)          # as transformers did: max_length=256
            tokenizer.enable_padding()
            options = onnxruntime.SessionOptions()
            options.log_severity_level = 3            # warnings about unused initializers: noise
            # it runs beside the user's work all day: the arena kept a batch's peak (786 MB ->
            # 2.9 GB after 128 long texts; 810 MB without, 4% slower), and every core busy
            # froze the PC while a backlog was embedded
            options.enable_cpu_mem_arena = False
            options.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
            LocalEmbedder._tok = tokenizer
            LocalEmbedder._model = onnxruntime.InferenceSession(
                model_file, options, providers=["CPUExecutionProvider"])
            LocalEmbedder._loaded.set()

    def ready(self, timeout) -> bool:
        """Whether the model is loaded, waiting at most `timeout` seconds. The first call starts
        loading it in the background, so a later question finds it ready."""
        if LocalEmbedder._model is not None or _service.connected(self.model_name):
            return True
        if LocalEmbedder._loading.acquire(blocking=False):
            def load():
                try:
                    with LocalEmbedder._lock:
                        self._load()
                except Exception:
                    pass                     # no model on disk: word search keeps answering
                finally:
                    LocalEmbedder._loading.release()
            threading.Thread(target=load, name="graph-mind-model", daemon=True).start()
        return LocalEmbedder._loaded.wait(timeout)

    def _key(self, text):
        return hashlib.sha256((self.model_name + "\0" + text).encode("utf-8")).hexdigest()

    def _encode(self, texts):
        import numpy as np
        encoded = LocalEmbedder._tok.encode_batch(texts)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in {i.name for i in LocalEmbedder._model.get_inputs()}:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = LocalEmbedder._model.run(None, feed)[0]
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(1) / np.clip(weights.sum(1), 1e-9, None)
        pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        return pooled.tolist()

    def _encode_paced(self, texts, backlog=False):
        """A question's few texts at once. A chunk of a backlog (all memory again after a model
        change) takes this process's one backlog lane and, when PACE is set, keeps it for a rest
        as long as the chunk took: the model's threads half busy, and a question waits behind
        one chunk (~5 s), never a whole batch."""
        if not texts:
            return []
        if not backlog:
            with LocalEmbedder._lock:
                self._load()
                return self._encode(texts)
        with LocalEmbedder._backlog:
            began = time.monotonic()
            with LocalEmbedder._lock:
                self._load()
                vectors = self._encode(texts)
            if LocalEmbedder.PACE:
                time.sleep(time.monotonic() - began)
        return vectors

    def _keep(self, texts, vectors):
        with LocalEmbedder._lock:
            for text, vector in zip(texts, vectors):
                self.cache.put(self._key(text), vector)
                self._since += 1
            if self._since >= LocalEmbedder.FLUSH_EVERY:
                self.flush()

    def embed(self, texts):
        texts = [t.strip()[:2000] for t in texts]
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.cache]
        if not missing:
            return [self.cache.get(self._key(t)) for t in texts]
        served = _service.encode(self.model_name, missing)     # outside the lock: no waiting
        if served is not None:
            self._keep(missing, served)
        else:
            size = LocalEmbedder.CHUNK if LocalEmbedder.PACE else 128   # 128: benchmarks' speed
            for start in range(0, len(missing), size):
                # the service is asked the same backlog by every app's server: what another
                # thread embedded meanwhile is skipped, each chunk kept as soon as it is done
                chunk = [t for t in missing[start:start + size] if self._key(t) not in self.cache]
                self._keep(chunk, self._encode_paced(chunk, backlog=len(missing) > size))
        return [self.cache.get(self._key(t)) for t in texts]

    def flush(self):
        with LocalEmbedder._lock:
            self.cache.flush()
            self._since = 0

    def verify(self, texts, sample=16) -> bool:
        """Are the cached vectors this model's? A few cached texts are embedded afresh; if more
        than one comes back different, the whole cache is cleared (every process sharing it
        drops it too) and refilled. True when it was cleared. Caches written before 0.9 could
        pair keys with other texts' vectors when processes saved at the same moment: the
        bench's had cosine ~0.07 with the model's, and search by meaning was quietly wrong."""
        import random

        import numpy as np
        known = [t for t in dict.fromkeys(t.strip()[:2000] for t in texts)
                 if self._key(t) in self.cache]
        if len(known) < 4:
            return False
        picked = random.sample(known, min(sample, len(known)))
        with LocalEmbedder._lock:
            self._load()
            fresh = np.asarray(self._encode(picked), dtype=np.float32)
        cached = np.asarray([self.cache.get(self._key(t)) for t in picked], dtype=np.float32)
        cosines = (fresh * cached).sum(1) / np.clip(
            np.linalg.norm(fresh, axis=1) * np.linalg.norm(cached, axis=1), 1e-9, None)
        if int((cosines < 0.99).sum()) <= 1:
            return False
        self.cache.clear()
        return True


def service_file() -> Path:
    from graph_mind.paths import graph_mind_home
    return graph_mind_home() / "embedder.json"


class _Service:
    """This process's line to the capture service's loaded model, if one is running. Any failure
    (no service, a stale file, the service gone mid-call) means: load the model here, as before,
    and look for the service again a little later."""
    RETRY = 30.0

    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None
        self.retry_at = 0.0

    def _open(self, model):
        from multiprocessing.connection import Client
        info = json.loads(service_file().read_text(encoding="utf-8"))
        if info["model"] != model:
            _replace_old_service(info, model)
            raise LookupError("the service runs another model")
        return Client(("127.0.0.1", info["port"]), authkey=bytes.fromhex(info["key"]))

    def connected(self, model) -> bool:
        if not LocalEmbedder.USE_SERVICE:
            return False
        with self.lock:
            if self.conn is None and time.monotonic() >= self.retry_at:
                try:
                    self.conn = self._open(model)
                except Exception:
                    self.retry_at = time.monotonic() + self.RETRY
            return self.conn is not None

    def encode(self, model, texts):
        if not self.connected(model):
            return None
        with self.lock:
            try:
                self.conn.send(texts)
                return self.conn.recv()
            except Exception:
                self.conn, self.retry_at = None, time.monotonic() + self.RETRY
                return None


_service = _Service()


def _replace_old_service(info, model) -> None:
    """The capture service lends an older version's model: this process is newer (Graph-MIND was
    updated while it ran). Restart it on this version, or every app's server would load its own
    copy of the new model, ~700 MB each. An older server never restarts a newer service."""
    if info.get("model") not in OLD_MODELS or model != LOCAL_MODEL:
        return
    try:
        import psutil
        old = psutil.Process(int(info["pid"]))
        if "capture_cli" in " ".join(old.cmdline()):
            old.terminate()
            old.wait(15)
        from graph_mind.automatic_capture import ensure_service
        ensure_service()
    except Exception:                 # gone already, or not ours: the next try looks again
        pass


def serve(embedder) -> None:
    """Lend this process's model to the memory servers on this PC (the capture service calls it).

    Local only: 127.0.0.1, and a client must prove it read the key in the user's own
    ~/.graph-mind/embedder.json. Texts come in, vectors go out; nothing is stored here."""
    from multiprocessing.connection import Listener
    key = os.urandom(32)
    listener = Listener(("127.0.0.1", 0), authkey=key)
    path = service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"port": listener.address[1], "key": key.hex(),
                                     "model": embedder.model_name, "pid": os.getpid()}),
                         encoding="utf-8")
    os.replace(temporary, path)

    def answer(conn):
        with conn:
            while True:
                try:
                    texts = conn.recv()
                except (EOFError, OSError):
                    return
                conn.send([list(v) for v in embedder.embed(texts)])   # cached: asked once

    def accept():
        while True:
            try:
                conn = listener.accept()
            except Exception:             # a client with the wrong key, or one that hung up
                continue
            threading.Thread(target=answer, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, name="graph-mind-embedder", daemon=True).start()


def cosine(a, b):
    dot = sum(i * j for i, j in zip(a, b))
    na = math.sqrt(sum(i * i for i in a))
    nb = math.sqrt(sum(j * j for j in b))
    return dot / (na * nb) if na and nb else 0.0


def default_embedder(db_path):
    """Local, on-device embedder with a cache persisted beside the memory DB."""
    # a new name per model: the old cache holds another model's vectors (384 dims, not 768),
    # and servers of the old version still running write to it
    db_path = Path(db_path)
    return LocalEmbedder(db_path.with_name(db_path.stem + ".semantic-embed-mpnet8.json"))
