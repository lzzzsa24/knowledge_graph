"""Exercise concurrent document encoders without loading a CUDA model."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np

from kg_pilot import native_embeddings as embeddings


def test_prepared_vectors_survive_reopen_preserve_order_and_bind_revision(tmp_path, monkeypatch):
    config = tmp_path/"model_config.json"
    config.write_text('{"revision":"one"}')
    path = tmp_path/"embedding-cache.sqlite3"
    batches = []
    def fake_encode(model, texts):
        batches.append(list(texts))
        return np.array([[int(x)]*1024 for x in texts],dtype=np.float32)
    monkeypatch.setattr(embeddings, "load_encoder", lambda *_: object())
    monkeypatch.setattr(embeddings, "encode", fake_encode)
    def run(texts):
        encoder = embeddings.NativeEncoder(tmp_path, "cpu", cache_path=path)
        try:
            values = encoder.encode(texts)
            np.testing.assert_array_equal(values[:,0],list(map(int,texts)))
            return encoder.metrics()
        finally:
            encoder.close()
    run(["1","2"])
    metrics = run(["2","3","1","2"])
    assert metrics["cached_rows"]==3 and metrics["encoded_rows"]==1
    assert batches==[["1","2"],["3"]]
    config.write_text('{"revision":"two"}')
    assert run(["2"])["cached_rows"]==0
    assert batches[-1]==["2"]


def test_busy_worker_reconnects_to_same_live_process(tmp_path, monkeypatch):
    import psutil
    (tmp_path/"model_config.json").write_text('{"revision":"test"}')
    enc=embeddings.NativeEncoder(tmp_path,"cuda")
    enc.root.mkdir(parents=True)
    endpoint=dict(pid=os.getpid(),created=psutil.Process().create_time(),port=1234,revision="test")
    (enc.root/"embedding-worker.json").write_text(json.dumps(endpoint))
    attempts=[]
    def connect(address,timeout):
        attempts.append(address)
        if len(attempts)<3:
            raise OSError("busy")
        return "connected"
    monkeypatch.setattr(embeddings.socket,"create_connection",connect)
    assert enc._connect(time.monotonic()+5)=="connected"
    assert attempts==[("127.0.0.1",1234)]*3


def test_cuda_batches_share_a_worker_and_restart_after_idle(tmp_path, monkeypatch):
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    (pilot / "model_config.json").write_text('{"revision": "fake"}', encoding="utf8")
    module_root = Path(embeddings.__file__).parents[1]
    stub = """
import sys
import time
from pathlib import Path
import numpy as np
sys.path.insert(0, sys.argv[1])
from kg_pilot import native_embeddings as embeddings
embeddings.load_cuda_encoder = lambda path: (object(), {"device":"cuda", "precision":"float32"})
def encode(model, texts):
    time.sleep(0.04)
    return np.array([[float(text)] * 1024 for text in texts], dtype=np.float32)
embeddings.encode = encode
embeddings.serve(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], idle_timeout=1)
"""
    processes = []

    def launch(args, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", stub, str(module_root), *args[-3:]], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(embeddings, "subprocess", SimpleNamespace(Popen=launch, DEVNULL=subprocess.DEVNULL))
    encoders = [embeddings.NativeEncoder(pilot, "cuda") for _ in range(3)]
    assert not processes
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(encoder.encode, [str(i)]) for i, encoder in enumerate(encoders)]
            assert [result.result(timeout=20)[0, 0] for result in futures] == [0, 1, 2]
        assert len(processes) == 1
        assert all(encoder.metrics()["gpu_seconds"] > 0 for encoder in encoders)
        assert all(encoder.metrics()["calls"] == 1 for encoder in encoders)
        endpoint = encoders[0].root / "embedding-worker.json"
        first = json.loads(endpoint.read_text(encoding="utf8"))
        assert processes[0].wait(timeout=8) == 0
        np.testing.assert_array_equal(encoders[0].encode(["7"]), np.full((1, 1024), 7))
        assert len(processes) == 2
        assert json.loads(endpoint.read_text(encoding="utf8"))["token"] != first["token"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
