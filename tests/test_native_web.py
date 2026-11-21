"""Web-native retrieval never falls back to the API and binds quotes to the cited page."""
import hashlib
import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

from web import native, server
from kg_pilot import codex_subscription, qa


@pytest.fixture
def hit(tmp_path):
    path = tmp_path / "collection" / "sample.pdf"
    path.parent.mkdir()
    with pymupdf.open() as pdf:
        for text in ("The first page contains the title.", "The measured resistance was five ohms."):
            pdf.new_page().insert_text((40, 60), text)
        pdf.save(path)
    return {"doc_id": "test", "title": "Sample", "page": 2, "page_end": 2,
            "pdf": str(path), "pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "chunk_id": "chunk-test", "excerpt": "Index description", "score": 0.65}


def test_search_returns_page_range_and_revision_without_path(monkeypatch, hit):
    monkeypatch.setattr(native, "search", lambda *args: {"hits": [hit]})
    response = TestClient(server.app).get("/api/native/search", params={"q": "resistance"})
    assert response.status_code == 200
    row, = response.json()
    assert (row["method"], row["pdf_page"], row["page_end"], row["pdf_sha256"]) == ("native", 2, 2, hit["pdf_sha256"])
    assert "pdf" not in row


@pytest.mark.parametrize("quote,expected", [("measured resistance was five ohms", 200), ("first page contains", 503)])
def test_api_requires_quote_on_same_page(monkeypatch, tmp_path, hit, quote, expected):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    monkeypatch.setattr(native, "search", lambda *a, **kw: {"generation": 3, "hits": [hit]})
    def subscription(messages, *args):
        assert messages[-1]["content"][-1]["type"] == "image_url"
        return json.dumps({"answer": "五欧姆。[S1]", "answerability": "supported", "quotes": {"S1": quote}}), {}, "test"
    monkeypatch.setattr(codex_subscription, "run_subscription", subscription)
    response = TestClient(server.app).post("/api/native/chat", json={"question": "测量结果？"})
    assert response.status_code == expected
    if expected == 200:
        source, = response.json()["sources"]
        assert (source["verified_quote"], source["pdf_page"], source["pdf_sha256"]) == (quote, 2, hit["pdf_sha256"])


def test_changed_original_or_cross_origin_never_calls_cli(monkeypatch, tmp_path, hit):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    monkeypatch.setattr(native, "search", lambda *a, **kw: {"generation": 3, "hits": [{**hit, "pdf_sha256": "old"}]})
    monkeypatch.setattr(codex_subscription, "run_subscription", lambda *a: pytest.fail("Unexpected subscription call"))
    client = TestClient(server.app)
    assert client.post("/api/native/chat", json={"question": "问"}).status_code == 503
    assert client.post("/api/native/chat", json={"question": "问"}, headers={"origin": "https://other.example"}).status_code == 403


def test_native_api_uses_configured_model_and_same_page_text(monkeypatch, tmp_path, hit):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    monkeypatch.setattr(native, "search", lambda *a, **kw: {"generation": 2, "hits": [hit]})
    monkeypatch.setattr(qa, "generation_settings", lambda channel: ({"provider": "mock", "model": "test"},
                        {"provider_id": "openai_compatible"}, "test-key") if channel == "api" else pytest.fail(channel))
    def complete(method, endpoint, key, payload, **kw):
        assert isinstance(payload["messages"][-1]["content"], str) and key == "test-key"
        assert "measured resistance" in payload["messages"][-1]["content"]
        return {"choices": [{"message": {"content": json.dumps({"answer": "五欧姆。[S1]", "quotes": {"S1": "five ohms"}})}}]}
    monkeypatch.setattr(qa, "_request", complete)
    result = TestClient(server.app).post("/api/native/chat", json={"question":"测量结果？","channel":"api"})
    assert result.status_code == 200 and result.json()["sources"][0]["pdf_page"] == 2


def test_demo_subscription_uses_demo_evidence_only(monkeypatch):
    source = {"id":"S1", "doc_id":"D", "title":"Demo", "pdf_page":1, "text":"Demo evidence.", "evidence_id":"demo:1"}
    monkeypatch.setattr(qa, "retrieve_sources", lambda *a: ([source], []))
    monkeypatch.setattr(qa, "_format_event", lambda **kw: None)
    monkeypatch.setattr(qa, "provider_status", lambda: pytest.fail("Read API configuration"))
    monkeypatch.setattr(native, "search", lambda *a, **kw: pytest.fail("Used native retrieval"))
    def complete(messages, *a):
        assert "Demo evidence" in messages[-1]["content"]
        return '{"answer":"有证据。[S1]","answerability":"supported"}', {}, "sol"
    monkeypatch.setattr(codex_subscription, "run_subscription", complete)
    response = TestClient(server.app).post("/api/chat", json={"question":"结果？","channel":"subscription"})
    assert response.status_code == 200 and response.json()["sources"] == [source]


def test_queue_action_is_explicit_and_local(monkeypatch):
    calls = []
    monkeypatch.setattr(native, "queue_command", lambda pilot, action: calls.append(action))
    client = TestClient(server.app)
    assert client.get("/api/native/queue").status_code == 405
    assert client.post("/api/native/queue", json={"action":"resume"}, headers={"origin":"https://other.example"}).status_code == 403
    assert not calls
    assert client.post("/api/native/queue", json={"action":"pause"}).status_code == 202
    assert calls == ["pause"]


def test_concurrency_saves_without_starting_or_pausing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    monkeypatch.setattr(native, "queue_command", lambda *_: pytest.fail("Settings must not start a process"))
    root = native.native_library.directory(tmp_path)
    url = "/api/native/queue/settings"
    client = TestClient(server.app)
    assert client.post(url, json={"documents": 3}, headers={"origin": "https://other.example"}).status_code == 403
    for value in [0, 5, 1.5, True, "3"]:
        assert client.post(url, json={"documents": value}).status_code == 422
    assert not root.exists()
    assert client.post(url, json={"documents": 3}).json() == {"documents": 3}
    assert native.native_library.document_concurrency(root) == 3
    assert {p.name for p in root.iterdir()} == {"queue_settings.json"}


def test_paper_progress_uses_revision_and_published_manifest(tmp_path):
    lib = native.native_library
    row = {"id":"paper", "title":"Test paper", "sha256":"v2", "pages":2}
    job = tmp_path / "documents/paper"
    lane = {"status":"running", "phase":"publish", "started":"2", "sha256":"v2"}
    parsed = {"identity":{"sha256":"v1"}, "items":[{"type":"text"}, {"type":"image"}], "pages":[{},{}]}
    lib.save(job / "mixed-parse.json", parsed)
    assert native._paper_progress(tmp_path, row, lane)["parsed_pages"] is None
    parsed["identity"]["sha256"] = "v2"
    lib.save(job / "mixed-parse.json", parsed)
    lib.save(job / "responses/one.json", {})
    lib.save(job / "index-stage.json", {"sha256":"v2","stage":"verified","at":"1"})
    current = native._paper_progress(tmp_path, row, lane)
    assert (current["stage"], current["milestones"], current["parsed_pages"], current["visual_items"], current["responses"]) == ("indexing",1,2,1,1)
    lib.save(job / "index-stage.json", {"sha256":"v2","stage":"verified","at":"3"})
    assert native._paper_progress(tmp_path, row, lane)["stage"] == "publishing"
    assert native._paper_progress(tmp_path, row, {})["stage"] == "saved"
    assert native._paper_progress(tmp_path, row, lane, {"pages":2, "chunks":12})["milestones"] == 3


def test_elapsed_counts_processing_intervals_and_freezes_when_stopped(tmp_path):
    lib=native.native_library
    job=tmp_path/"paper"
    prep={"sha256":"rev","started":"2026-11-21T10:00:00Z","finished":"2026-11-21T10:01:00Z","prior_seconds":15}
    lane={"sha256":"rev","phase":"publish","status":"running","started":"2026-11-21T12:00:00Z"}
    lib.save(job/"lane-prepare.json",prep)
    lib.save(job/"lane-publish.json",lane)
    assert native._paper_elapsed(job,"rev",lane,None,"2026-11-21T12:00:30Z")==105
    lib.save(job/"lane-publish.json",dict(lane,status="interrupted",finished="2026-11-21T12:00:42Z"))
    assert native._paper_elapsed(job,"rev",{},None,"2026-11-22T10:00:00Z")==117
    assert native._paper_elapsed(job,"other",{},None,"2026-11-22T10:00:00Z") is None


def test_web_shows_only_the_live_selected_run_with_general_queue_paused(tmp_path):
    lib = native.native_library
    root = lib.directory(tmp_path)
    rows = [dict(id=k, title=k, prebuilt=False, group=1, pages=2, sha256=k * 64) for k in "ab"]
    lib.save(root / "queue.json", {"documents": rows})
    lib.save(root / "manifest.json", {"generation": 1, "documents": {}})
    lib.save(root / "scheduler.json", {"status": "paused"})
    marker = root / "pause.json"
    lib.save(marker, {"requested": "original"})
    proc = native.psutil.Process()
    lease = {"run_id": "sample", "pid": proc.pid, "created": proc.create_time(),
             "started": "2026-01-01", "pause_sha256": lib.file_hash(marker),
             "concurrency": 1, "documents": {"a": rows[0]["sha256"]}}
    lib.save(root / "selected-run.json", lease)
    progress = {"at": "2026-01-02", "run_id": "sample", "doc_ids": ["a", "b"]}
    lib.save(root / "selected-progress.json", progress)
    live = native.status(tmp_path, progress=True)
    assert live["phase"] == "running" and live["global_queue_paused"] and live["active_limit"] == 1
    assert [r["doc_id"] for r in live["active"]] == ["a"]
    lib.save(root / "selected-progress.json", dict(progress, run_id="previous"))
    assert not native.status(tmp_path)["active"]
    lib.save(marker, {"requested": "pause again"})
    assert native.status(tmp_path)["phase"] == "pausing"
    lib.save(root / "selected-run.json", dict(lease, created=0))
    assert native.status(tmp_path)["phase"] == "paused"


def test_crop_gallery_shows_only_index_input_and_checks_original_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    lib = native.native_library
    root = lib.directory(tmp_path)
    lib.save(root / "queue.json", {"documents":[{"id":"doc", "sha256":"revision", "pages":2, "title":"Paper"}]})
    job = root / "documents/doc"
    image = job / "original-regions/fig.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"test crop")
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    item = {"type":"image", "img_path":str(image), "image_sha256":sha, "page_idx":1, "image_caption":["Figure 1"]}
    parsed = {"identity":{"sha256":"revision"}, "items":[{"type":"text"}, item, dict(item, page_idx=0)],
              "skipped_decorations":[dict(item, reason="Journal masthead")]}
    lib.save(job / "mixed-parse.json", parsed)
    client = TestClient(server.app)
    base = "/api/native/documents/doc/regions"
    gallery = client.get(base).json()
    assert [(r["page"],r["caption"],r["sha256"]) for r in gallery["items"]] == [(2,"Figure 1",sha),(1,"Figure 1",sha)]
    assert [r["reason"] for r in gallery["skipped"]] == ["Journal masthead"]
    assert "path" not in json.dumps(gallery)
    url = f"{base}/{sha}.png"
    assert client.get(url).content == b"test crop"
    assert client.get(base, headers={"origin":"https://other.example"}).status_code == 403
    assert client.get(base+"/unknown.png").status_code == 404
    image.write_bytes(b"changed")
    assert client.get(url).status_code == 409
    outside = tmp_path / "unrelated.png"
    outside.write_bytes(b"test crop")
    item["img_path"] = str(outside)
    lib.save(job / "mixed-parse.json", parsed)
    assert client.get(url).status_code == 409
    parsed["identity"]["sha256"] = "old"
    lib.save(job / "mixed-parse.json", parsed)
    assert client.get(base).status_code == 409


def test_legacy_gallery_links_to_revision_scoped_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    lib = native.native_library
    root = lib.directory(tmp_path)
    row = {"id":"old", "sha256":"revision", "title":"Old paper", "pages":1}
    lib.save(root/"queue.json", {"documents":[row]})
    directory = root/"legacy_gallery/old/revision"
    image = directory/"original-regions/crop.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"preview")
    digest = hashlib.sha256(b"preview").hexdigest()
    lib.save(directory/"regions.json", {"identity":{"sha256":"revision"}, "items":[{"type":"table",
        "img_path":str(image), "page_idx":0, "image_sha256":digest, "preview_source":"pdf_crop"}]})
    client = TestClient(server.app)
    base = "/api/native/documents/old/regions"
    assert client.get(base).json()["items"][0]["preview_source"] == "pdf_crop"
    assert client.get(f"{base}/{digest}.png").content == b"preview"
    lib.save(root/"queue.json", {"documents":[dict(row, sha256="changed")]})
    assert client.get(base).status_code == 404


@pytest.mark.parametrize("channel", ["api", "subscription"])
@pytest.mark.parametrize("prompt", ["铜键合", ""])
def test_native_ideas_use_published_pages_for_either_channel(monkeypatch, tmp_path, hit, channel, prompt):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    root = native.native_library.directory(tmp_path)
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"documents":{"test":{"title":"Sample"}}}), encoding="utf8")
    def lookup(pilot, query, limit, *, doc_ids):
        assert doc_ids == (None if prompt else {"test"})
        return {"generation": 1, "hits":[hit]}
    monkeypatch.setattr(native, "search", lookup)
    monkeypatch.setattr(qa, "retrieve_sources", lambda *a: pytest.fail("Read Demo evidence"))
    monkeypatch.setattr(qa, "generation_settings", lambda via: ({"provider":via,"model":"test"},
                        {"provider_id":via}, None) if via == channel else pytest.fail(via))
    def complete(connection, key, payload, **kw):
        evidence = json.loads(payload["messages"][-1]["content"])["evidence"]
        assert evidence[0]["pdf_page"] == 2 and "five ohms" in evidence[0]["text"]
        assert connection["provider_id"] == channel
        idea = dict(title="电阻对照", question="温度有何影响？", hypothesis="温度会影响电阻。",
                    basis="文献测得五欧姆。[S1]", novelty="比较温度梯度", validation=["分组测量"],
                    risks=["误差"], search_queries=["resistance"])
        return {"choices":[{"message":{"content":json.dumps({"ideas":[idea]})}}]}
    monkeypatch.setattr(qa, "generate_completion", complete)
    response = TestClient(server.app).post("/api/native/ideas", json={"prompt":prompt,"count":1,"channel":channel})
    assert response.status_code == 200
    assert response.json()["ideas"][0]["source_ids"] == ["S1"]
    assert response.json()["sources"][0]["pdf_sha256"] == hit["pdf_sha256"]
