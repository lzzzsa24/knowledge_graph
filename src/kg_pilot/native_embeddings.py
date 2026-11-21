"""BGE-M3 encoders for native indexing, with one CUDA worker per library."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
from select import select
import socket
import sqlite3
from socketserver import StreamRequestHandler, TCPServer
import subprocess
import sys
import time
from threading import Lock, Thread

import numpy as np


START_TIMEOUT = 30
IDLE_TIMEOUT = 60
REQUEST_TIMEOUT = 540  # Leave time for cleanup before LightRAG's 600 s timeout.
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_TOKENS = 512
DIMENSIONS = 1024


def model_path(pilot):
    config = json.loads((pilot / "model_config.json").read_text(encoding="utf8"))
    return pilot / "model_cache/models--BAAI--bge-m3/snapshots" / config["revision"]


def load_encoder(path, device):
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(4)
    model = SentenceTransformer(str(path), device=device, local_files_only=True)
    model.max_seq_length = MAX_TOKENS
    return model


def encode(model, texts):
    return model.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False,
                        convert_to_numpy=True)


class NativeEncoder:
    def __init__(self, pilot, device, *, cache_path=None, cache_sources=()):
        from kg_pilot.native_library import directory

        self.lock = Lock()
        self.pilot = pilot
        self.root = directory(pilot)
        self.worker = None
        self.device = device
        self.snapshot = model_path(pilot)
        self.cache = None
        self.model = None
        self.cached_rows = 0
        self.encoded_rows = 0
        self.calls = 0
        self.seconds = 0.0
        self.gpu_seconds = 0.0
        self.identity = {"device": device, "precision": "float32"}
        if device not in {"cpu", "cuda"}:
            raise ValueError(f"Unsupported embedding device: {device}")
        if cache_path is not None:
            # Keep exact FP32 vectors in the document checkpoint, outside both indices.
            self.cache_prefix = json.dumps(["BAAI/bge-m3", self.snapshot.name, device, "float32",
                                            MAX_TOKENS, DIMENSIONS, "normalized-dense-v1"]).encode() + b"\0"
            self.cache = sqlite3.connect(cache_path, check_same_thread=False, uri=True)
            self.cache.execute("PRAGMA journal_mode=WAL")
            self.cache.execute("PRAGMA synchronous=NORMAL")
            self.cache.execute("CREATE TABLE IF NOT EXISTS vectors "
                               "(key BLOB PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID")
            self.cache_schemas = ["main"]
            for index, source in enumerate(cache_sources):
                if source.is_file():
                    schema = f"checkpoint_{index}"
                    self.cache.execute(f"ATTACH DATABASE ? AS {schema}", (source.resolve().as_uri()+"?mode=ro",))
                    self.cache_schemas.append(schema)

    def _start_worker(self, endpoint, deadline):
        import psutil

        token = secrets.token_hex(24)
        ready = endpoint.with_suffix("." + token + ".ready")
        python = self.pilot.parent / ".venv-bge-gpu/Scripts/python.exe"
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "0", "PYTHONIOENCODING": "utf-8"}
        with (self.root / "embedding-worker.log").open("ab") as log:
            process = subprocess.Popen(
                [str(python), "-X", "utf8", str(Path(__file__).resolve()), str(self.snapshot), str(ready), token],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        Thread(target=process.wait, daemon=True).start()
        deadline = min(deadline, time.monotonic() + START_TIMEOUT)
        try:
            while not ready.is_file():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("CUDA embedding worker failed to start; see embedding-worker.log")
                time.sleep(0.05)
            worker = json.loads(ready.read_text(encoding="utf8"))
            worker["created"] = psutil.Process(worker["pid"]).create_time()
            worker["revision"] = self.snapshot.name
            ready.write_text(json.dumps(worker), encoding="utf8")
            ready.replace(endpoint)
            return worker
        finally:
            ready.unlink(missing_ok=True)

    def _connect(self, deadline):
        import psutil
        from kg_pilot.ingestion import file_lock

        endpoint = self.root / "embedding-worker.json"
        delay = .1
        while time.monotonic() < deadline:
            previous = self.worker
            if previous:
                try:
                    return socket.create_connection(("127.0.0.1", previous["port"]),
                                                    timeout=min(2, max(.01, deadline-time.monotonic())))
                except OSError:
                    pass
            with file_lock(self.root / "embedding-worker-start.lock", timeout=max(0, deadline-time.monotonic())):
                if time.monotonic() >= deadline:
                    break
                worker = json.loads(endpoint.read_text(encoding="utf8")) if endpoint.is_file() else None
                self.worker = None
                if worker:
                    try:
                        process = psutil.Process(worker["pid"])
                        if process.create_time() != worker["created"]:
                            worker = None
                        elif "revision" not in worker:
                            # Older shared workers carried the snapshot on the command line.
                            worker["revision"] = Path(process.cmdline()[-3]).name
                    except psutil.NoSuchProcess:
                        worker = None
                    except psutil.AccessDenied:
                        # Cannot prove this worker is gone or using our snapshot.
                        worker = {"revision": None}
                if worker is None:
                    self.worker = self._start_worker(endpoint, deadline)
                elif worker.get("revision") == self.snapshot.name:
                    self.worker = worker
            if self.worker and self.worker != previous:
                continue
            # Another document may own the GPU or the TCP backlog may be full.
            # Leave the startup lock free; replace only a confirmed dead worker.
            time.sleep(min(delay, max(0, deadline-time.monotonic())))
            delay = min(2, delay * 2)
        raise TimeoutError("Timed out waiting for the shared CUDA embedding worker")

    def _encode_cuda(self, texts):
        # A queued connection can close between connect() and accept() when
        # the idle worker is exiting. A vector request is safe to send again.
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while time.monotonic() < deadline:
            with self._connect(deadline) as connection:
                connection.settimeout(max(.01, deadline-time.monotonic()))
                try:
                    with connection.makefile("rwb") as stream:
                        write_frame(stream, {"token": self.worker["token"], "texts": texts})
                        line = stream.readline(MAX_FRAME_BYTES)
                except ConnectionError:
                    line = b""
            if line:
                frame = json.loads(line)
                if frame.get("error"):
                    raise RuntimeError(frame["error"])
                identity = frame["identity"]
                if identity.get("device") != "cuda" or identity.get("precision") != "float32":
                    raise RuntimeError("Unexpected GPU embedding worker identity")
                self.identity = identity
                self.gpu_seconds += frame["seconds"]
                return np.frombuffer(base64.b64decode(frame["vectors"], validate=True), dtype="<f4").reshape(-1, 1024)
            self.worker = None
            time.sleep(min(.1, max(0, deadline-time.monotonic())))
        raise TimeoutError("Timed out waiting for a CUDA embedding response")

    def _compute(self, texts):
        if self.device == "cpu":
            if self.model is None:
                self.model = load_encoder(self.snapshot, "cpu")
            vectors = encode(self.model, texts)
        else:
            vectors = self._encode_cuda(texts)
        if vectors.shape != (len(texts), DIMENSIONS) or not np.isfinite(vectors).all():
            raise ValueError("Invalid BGE-M3 response dimensions or values")
        self.encoded_rows += len(texts)
        return vectors

    def _cached_encode(self, texts):
        keys = [hashlib.sha256(self.cache_prefix + text.encode("utf8")).digest() for text in texts]
        unique = dict(zip(keys, texts))
        rows = {}
        for key in unique:
            for schema in self.cache_schemas:
                entry = self.cache.execute(f"SELECT value FROM {schema}.vectors WHERE key=?", (key,)).fetchone()
                if entry and len(entry[0]) == DIMENSIONS * 4:
                    vector = np.frombuffer(entry[0], dtype="<f4")
                    if np.isfinite(vector).all():
                        rows[key] = vector
                        break
        missing = [key for key in unique if key not in rows]
        if missing:
            fresh = self._compute([unique[key] for key in missing])
            rows.update(zip(missing, fresh))
            with self.cache:
                self.cache.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?)",
                                       ((key, rows[key].astype("<f4", copy=False).tobytes()) for key in missing))
        self.cached_rows += len(keys) - len(missing)
        return np.stack([rows[key] for key in keys])

    def encode(self, texts):
        texts = list(texts)
        if not texts:
            return np.empty((0, DIMENSIONS), dtype=np.float32)
        with self.lock:
            start = time.perf_counter()
            vectors = self._cached_encode(texts) if self.cache is not None else self._compute(texts)
            self.calls += 1
            self.seconds += time.perf_counter() - start
            return vectors

    def metrics(self):
        result = {**self.identity, "calls": self.calls, "seconds": round(self.seconds, 3),
                  "cached_rows": self.cached_rows, "encoded_rows": self.encoded_rows}
        if self.device == "cuda":
            result["gpu_seconds"] = round(self.gpu_seconds, 3)
        return result

    def close(self):
        with self.lock:
            self.model = None
            if self.cache is not None:
                self.cache.close()
                self.cache = None


def write_frame(stream, frame):
    stream.write(json.dumps(frame, ensure_ascii=False).encode("utf8") + b"\n")
    stream.flush()


def load_cuda_encoder(path):
    import torch

    model = load_encoder(path, "cuda")
    param = next(model.parameters())
    if param.device.type != "cuda" or param.dtype != torch.float32:
        raise RuntimeError("BGE-M3 did not load on CUDA in FP32")
    identity = dict(device="cuda", gpu=torch.cuda.get_device_name(param.device),
                    precision="float32", torch=str(torch.__version__), cuda=torch.version.cuda)
    return model, identity


def serve(path, ready, token, idle_timeout=IDLE_TIMEOUT):
    model = None
    identity = None

    class Request(StreamRequestHandler):
        timeout = 5

        def handle(self):
            nonlocal model, identity
            try:
                line = self.rfile.readline(MAX_FRAME_BYTES)
            except OSError:
                return
            if not line.endswith(b"\n"):
                return
            try:
                request = json.loads(line)
                if request.get("token") != token:
                    return
                if model is None:
                    model, identity = load_cuda_encoder(path)
                start = time.perf_counter()
                vectors = encode(model, request["texts"])
                seconds = time.perf_counter() - start
                payload = base64.b64encode(vectors.astype("<f4", copy=False).tobytes()).decode("ascii")
                response = {"identity": identity, "seconds": seconds, "vectors": payload}
            except Exception as error:
                print(f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)
                response = {"error": f"CUDA embedding failed: {error}"}
            try:
                write_frame(self.wfile, response)
            except OSError:
                # The client may have stopped while this batch was in the queue.
                pass

    # A synchronous accept loop keeps exactly one batch on the GPU. Separate
    # document processes connect only for each batch, including during merges.
    with TCPServer(("127.0.0.1", 0), Request) as server:
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"port": server.server_address[1], "token": token, "pid": os.getpid()}),
            encoding="utf8",
        )
        temporary.replace(ready)
        while select([server], [], [], idle_timeout)[0]:
            server.handle_request()


if __name__ == "__main__":
    serve(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
