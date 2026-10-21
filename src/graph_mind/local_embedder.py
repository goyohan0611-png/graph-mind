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
    # Rest between backlog chunks (embed). The capture service and the MCP server set
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
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            options.add_session_config_entry("session.inter_op.allow_spinning", "0")
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
        if _service.holding():
            return False                  # the restarted service lends its model in a moment
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

    def _encode_now(self, texts):
        """Beside any backlog chunk, not after it: ONNX Runtime runs a session from several
        threads at once (one chunk held a question 5 s on CI)."""
        if not texts:
            return []
        with LocalEmbedder._lock:
            self._load()
        return self._encode(texts)

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
        if served is None and _service.holding():     # a restarted service: not a local copy
            time.sleep(max(0.0, _service.hold_until - time.monotonic()))
            served = _service.encode(self.model_name, missing)
        if served is not None:
            self._keep(missing, served)
            return [self.cache.get(self._key(t)) for t in texts]
        size = LocalEmbedder.CHUNK if LocalEmbedder.PACE else 128      # 128: benchmarks' speed
        if len(missing) <= size:                                      # a question: at once
            self._keep(missing, self._encode_now(missing))
            return [self.cache.get(self._key(t)) for t in texts]
        for start in range(0, len(missing), size):
            # A backlog, one chunk at a time in this process, each followed by a rest as long as
            # it took when PACE is set. The service is asked the same backlog by every app's
            # server: what another request embedded while this one waited is skipped.
            with LocalEmbedder._backlog:
                chunk = [t for t in missing[start:start + size] if self._key(t) not in self.cache]
                began = time.monotonic()
                self._keep(chunk, self._encode_now(chunk))
                if LocalEmbedder.PACE and chunk:
                    time.sleep(time.monotonic() - began)
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
        # The int8 model's vector for a text moves a little with the rest of its batch (dynamic
        # quantization: cosine 0.99-0.998), so "0.99, at most one below" wiped the whole cache at
        # every service start and re-embedded ~11k pieces for an hour. Corrupt caches sit near 0.
        # "not >= 0.9", not "< 0.9": NaN compares false both ways, and a cache of NaN
        # vectors passed as healthy
        if int((~(cosines >= 0.9)).sum()) <= len(picked) // 2:
            return False
        self.cache.clear()
        return True


def service_file() -> Path:
    from graph_mind.paths import graph_mind_home
    return graph_mind_home() / "embedder.json"


class _Service:
    """This process's line to the capture service's loaded model, if one is running. Any failure
    (no service, a stale file, the service gone mid-call) means: load the model here, as before,
    and look for the service again a little later. One connection per thread: a question never
    waits behind the backlog another thread of the same server sent."""
    RETRY = 30.0
    DEADLINE = 600.0          # a live service that stopped answering: give up on this request
    HOLD = 60.0               # after restarting an old service: wait for it, load no copy here

    def __init__(self):
        self.lock = threading.Lock()
        self.local = threading.local()
        self.retry_at = 0.0
        self.hold_until = 0.0

    def holding(self) -> bool:
        return time.monotonic() < self.hold_until

    def _open(self, model):
        from multiprocessing.connection import Client
        info = json.loads(service_file().read_text(encoding="utf-8"))
        if info["model"] != model:
            if _replace_old_service(info, model):
                self.hold_until = time.monotonic() + self.HOLD
            raise LookupError("the service runs another model")
        return Client(("127.0.0.1", info["port"]), authkey=bytes.fromhex(info["key"]))

    def connected(self, model) -> bool:
        if not LocalEmbedder.USE_SERVICE:
            return False
        if getattr(self.local, "conn", None) is not None:
            return True
        with self.lock:
            if time.monotonic() < self.retry_at:
                return False
            try:
                self.local.conn = self._open(model)
                self.hold_until = 0.0
            except Exception:
                # while holding, look again every 2 s: the new service writes its file soon
                self.retry_at = time.monotonic() + (2.0 if self.holding() else self.RETRY)
                self.local.conn = None
        return self.local.conn is not None

    def encode(self, model, texts):
        if not self.connected(model):
            return None
        conn = self.local.conn
        try:
            conn.send(texts)
            if not conn.poll(self.DEADLINE):
                raise TimeoutError("the service stopped answering")
            return conn.recv()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            self.local.conn, self.retry_at = None, time.monotonic() + self.RETRY
            return None


_service = _Service()


def _replace_old_service(info, model) -> bool:
    """The capture service lends an older version's model: this process is newer (Graph-MIND was
    updated while it ran). Restart it on this version, or every app's server would load its own
    copy of the new model, ~700 MB each. An older server never restarts a newer service."""
    if info.get("model") not in OLD_MODELS or model != LOCAL_MODEL:
        return False
    try:
        import psutil
        old = psutil.Process(int(info["pid"]))
        if "capture_cli" in " ".join(old.cmdline()):
            old.terminate()
            old.wait(15)
        from graph_mind.automatic_capture import ensure_service
        ensure_service()
        return True
    except Exception:                 # gone already, or not ours: the next try looks again
        return False


def serve(embedder) -> None:
    """Lend this process's model to the memory servers on this PC (the capture service calls it).

    Local only: 127.0.0.1, and a client must prove it read the key in the user's own
    ~/.graph-mind/embedder.json. Texts come in, vectors go out; nothing is stored here."""
    from multiprocessing.connection import Listener, answer_challenge, deliver_challenge
    key = os.urandom(32)
    # no authkey here: the Listener checks it inside accept(), in the one accepting thread, so a
    # local client that connected and stayed silent kept every memory server out. Each
    # connection is checked in its own thread instead.
    listener = Listener(("127.0.0.1", 0))
    path = service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    # readable by this user only: the key admits a client, and another account on the PC that
    # read it could send the service pickled objects
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                   encoding="utf-8") as handle:
        handle.write(json.dumps({"port": listener.address[1], "key": key.hex(),
                                 "model": embedder.model_name, "pid": os.getpid()}))
    os.replace(temporary, path)

    def answer(conn):
        with conn:
            try:
                deliver_challenge(conn, key)
                answer_challenge(conn, key)
            except Exception:             # the wrong key, or one that hung up
                return
            while True:
                try:
                    texts = conn.recv()
                except (EOFError, OSError):
                    return
                if not (isinstance(texts, list) and all(isinstance(t, str) for t in texts)):
                    return
                conn.send([list(v) for v in embedder.embed(texts)])   # cached: asked once

    def accept():
        while True:
            try:
                conn = listener.accept()
            except Exception:             # one that hung up at once
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
