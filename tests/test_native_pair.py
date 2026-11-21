from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading

import pytest

from kg_pilot import native_library as lib
from kg_pilot import native_pair as pair
from scripts import run_native_library as runner


def test_drain_waits_for_both_exact_revisions(tmp_path):
    wanted={"a":"a1","b":"b1"}
    lib.save(tmp_path/"pause_after_group.json",{"documents":wanted})
    for published in ({"a":{"sha256":"a1"}},{"a":{"sha256":"a1"},"b":{"sha256":"older"}}):
        lib.save(tmp_path/"manifest.json",{"documents":published})
        lib.check_pause(tmp_path)
        assert not (tmp_path/"pause.json").exists()
    lib.save(tmp_path/"manifest.json",{"documents":{doc:{"sha256":sha} for doc,sha in wanted.items()}})
    with pytest.raises(lib.PauseRequested):
        lib.check_pause(tmp_path)
    assert set(lib.read_json(tmp_path/"pause.json")["documents"])==set(wanted)


@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("production", [False, True])
def test_lanes_overlap_and_second_publishes_against_latest(tmp_path, monkeypatch, fail_first, production):
    root = lib.directory(tmp_path)
    rows = [dict(id=name, sha256=name, group=1, prebuilt=False) for name in ("a", "b")]
    lib.save(root / "queue.json", {"documents": rows})
    lib.save(root / "manifest.json", {"documents": {}, "generation": 7})
    monkeypatch.setattr(lib, "verify_source", lambda *_: None)
    pair.arm_pair(tmp_path, ["a", "b"])
    trial = lib.read_json(root / "two_lane_trial.json")
    overlap = threading.Barrier(2)
    monkeypatch.setattr(pair, "ProcessPoolExecutor",
                        lambda max_workers, **kwargs: ThreadPoolExecutor(max_workers=max_workers))

    def lane(pilot, row, phase):
        if row["id"] == "a" or phase == "prepare":
            overlap.wait(timeout=3)
        if phase == "prepare":
            return {"status": "prepared"}
        if fail_first and row["id"] == "a":
            raise RuntimeError("provider unavailable")
        manifest = lib.read_json(root / "manifest.json")
        if row["id"] == "b":
            assert ("a" in manifest["documents"]) != fail_first
        manifest["documents"][row["id"]] = row
        manifest["generation"] += 1
        lib.save(root / "manifest.json", manifest)
        return {"status": "published"}

    monkeypatch.setattr(pair, "_run_lane", lane)
    if production:
        report=pair.run_queue_pair(tmp_path,["a","b"])
        assert report["published"]==(["b"] if fail_first else ["a","b"])
        assert bool(report["errors"])==fail_first
        assert (root/report["report"]).is_file()
    elif fail_first:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            pair.run_selected_pair(tmp_path, "a", trial)
    else:
        assert pair.run_selected_pair(tmp_path, "a", trial)["status"] == "published"
    assert "b" in lib.read_json(root / "manifest.json")["documents"]
    assert pair.run_selected_pair(tmp_path, "a", trial) is None


def test_changed_or_unapproved_trial_revision_rejected(tmp_path, monkeypatch):
    root = lib.directory(tmp_path)
    rows = [dict(id="a", sha256="first", group=1, prebuilt=False),
            dict(id="b", sha256="marked", group=9, prebuilt=False)]
    lib.save(root / "queue.json", {"documents": rows})
    monkeypatch.setattr(lib, "verify_source", lambda *_: None)
    with pytest.raises(ValueError, match="authorized"):
        pair.select_rows(tmp_path, rows)
    lib.save(root / "marked_revisions.json", {"b": "marked"})
    with pytest.raises(ValueError, match="authorized"):
        pair.select_rows(tmp_path, [rows[0], dict(rows[1], sha256="changed")])


def test_production_groups_respect_scope_and_trailing_single():
    a,b,c,d,e=[{"id":x,"sha256":x} for x in "abcde"]
    assert list(runner.groups_for_run([a,b,c,d,e]))==[[a,b],[c,d],[e]]
    assert list(runner.groups_for_run([a],[a,c]))==[[a]]
    assert list(runner.groups_for_run([a,b,c,d,e],[b,d]))==[[a],[b,d],[c,e]]


def test_saved_width_is_read_between_groups(tmp_path):
    rows = [{"id": str(i), "sha256": str(i)} for i in range(8)]
    groups = runner.groups_for_run(rows, width=lambda: lib.document_concurrency(tmp_path))
    assert next(groups) == rows[:2]
    lib.save(tmp_path / "queue_settings.json", {"documents": 4})
    assert next(groups) == rows[2:6]
    lib.save(tmp_path / "queue_settings.json", {"documents": 1})
    assert list(groups) == [[rows[6]], [rows[7]]]
    assert list(runner.groups_for_run(rows[:2], rows[:2], width=lambda: 1)) == [[rows[0]], [rows[1]]]


@pytest.mark.parametrize("width", [3, 4])
def test_group_prepares_concurrently_then_publishes_in_order(tmp_path, monkeypatch, width):
    root = lib.directory(tmp_path)
    ids = list("abcd"[:width])
    rows = [dict(id=i, sha256=i, prebuilt=False, group=1) for i in ids]
    lib.save(root / "manifest.json", {"documents": {}, "generation": 0})
    lib.save(root / "queue.json", {"documents": rows})
    monkeypatch.setattr(lib, "verify_source", lambda *_: None)
    monkeypatch.setattr(pair, "ProcessPoolExecutor", lambda max_workers, **_: ThreadPoolExecutor(max_workers))
    monkeypatch.setattr(runner, "current_windows", lambda _: {})
    overlap = threading.Barrier(width)
    published = []
    def lane(pilot, row, phase):
        if phase == "prepare" or row["id"] == ids[0]:
            overlap.wait(timeout=3)
        if phase == "publish":
            published.append(row["id"])
            lib.save(root / "manifest.json", {"documents": {i: {"sha256": i} for i in published}, "generation": len(published)})
        return {"status": "prepared" if phase == "prepare" else "published"}
    monkeypatch.setattr(pair, "_run_lane", lane)
    async def call(name, args):
        assert name == "native_library_ingest_group"
        return {"data": pair.run_queue_group(tmp_path, args["doc_ids"])}
    assert asyncio.run(runner.ingest_group(root, rows, call, False)) == (0, [])
    assert published == ids


def test_quota_retry_resumes_only_unpublished_revision(tmp_path,monkeypatch):
    manifest=tmp_path/"manifest.json"
    a,b=[{"id":x,"sha256":x} for x in "ab"]
    lib.save(manifest,{"documents":{}})
    windows=iter([{},{"codex":{"primary":{"usedPercent":100,"resetsAt":10**10}}},{}])
    monkeypatch.setattr(runner,"current_windows",lambda root:next(windows))
    waits=[]
    async def wait(root,until,doc):
        waits.append(doc)
    monkeypatch.setattr(runner,"wait_for_window",wait)
    calls=[]
    async def call(name,args):
        calls.append((name,args))
        if len(calls)==1:
            lib.save(manifest,{"documents":{"a":a}})
            return {"ok":True,"data":{"published":["a"],"errors":{"B_prepare":"usage limit"}}}
        lib.save(manifest,{"documents":{"a":a,"b":b}})
        return {"ok":True,"data":{"status":"published"}}
    assert asyncio.run(runner.ingest_group(tmp_path,[a,b],call,True))==(0,[])
    assert waits==["b"]
    assert calls==[("native_library_ingest_pair",{"doc_ids":["a","b"]}),
                   ("native_library_ingest",{"doc_id":"b","single_only":True})]
    with pytest.raises(ValueError,match="revision differs"):
        runner.pending_rows(tmp_path,[dict(b,sha256="changed")])


def test_pair_error_cannot_turn_partial_publication_into_success(tmp_path,monkeypatch):
    a,b=[{"id":x,"sha256":x} for x in "ab"]
    lib.save(tmp_path/"manifest.json",{"documents":{}})
    monkeypatch.setattr(runner,"current_windows",lambda root:{})
    async def call(name,args):
        lib.save(tmp_path/"manifest.json",{"documents":{"b":b}})
        return {"ok":True,"data":{"published":["b"],"errors":{"A":"bad source page"}}}
    code,failures=asyncio.run(runner.ingest_group(tmp_path,[a,b],call,True))
    assert code==1
    assert [(f["doc_id"],f["message"]) for f in failures]==[("a","bad source page")]


def test_rolling_refills_before_slow_writer_finishes(tmp_path, monkeypatch):
    rows={doc:{"id":doc,"sha256":doc} for doc in "abcde"}
    manifest=tmp_path/"manifest.json"
    lib.save(manifest,{"documents":{}})
    lib.save(tmp_path/"queue_settings.json",{"documents":3})
    monkeypatch.setattr(runner,"current_windows",lambda _: {})
    active={}
    starts=[]
    published=[]

    async def scenario():
        first_three=asyncio.Event()
        refilled=asyncio.Event()

        async def call(name,args):
            assert name=="native_library_run_lane"
            doc,phase=args["doc_id"],args["phase"]
            assert doc not in active
            active[doc]=phase
            starts.append((doc,phase))
            assert len(active)<=3
            assert list(active.values()).count("publish")<=1
            if len(active)==3:
                first_three.set()
            if doc=="d" and phase=="prepare":
                assert active=={"a":"publish","c":"prepare","d":"prepare"}
                refilled.set()
            if (doc,phase)==("b","prepare"):
                await first_three.wait()
            if doc in {"a","c"}:
                await refilled.wait()
            if phase=="publish":
                published.append(doc)
                lib.save(manifest,{"documents":{doc:rows[doc] for doc in published}})
            active.pop(doc)
            return {"ok":True,"data":{"status":"prepared" if phase=="prepare" else "published"}}

        return await asyncio.wait_for(runner.ingest_rolling(tmp_path,list(rows.values()),call,True),5)

    assert asyncio.run(scenario())==(0,[])
    assert starts[:3]==[("a","publish"),("b","prepare"),("c","prepare")]
    assert set(published)==set(rows)


def test_rolling_refills_after_isolated_worker_exit(tmp_path):
    rows={doc:{"id":doc,"sha256":doc} for doc in "abc"}
    manifest=tmp_path/"manifest.json"
    lib.save(manifest,{"documents":{}})
    starts=[]

    async def scenario():
        refilled=asyncio.Event()

        async def call(_,args):
            doc,phase=args["doc_id"],args["phase"]
            starts.append((doc,phase))
            if doc=="b":
                assert phase=="prepare"
                return {"ok":False,"error":{"code":lib.PREPARE_WORKER_EXITED,"message":"worker exited"}}
            if (doc,phase)==("c","prepare"):
                refilled.set()
                return {"ok":True,"data":{"status":"prepared"}}
            if doc=="a":
                await asyncio.wait_for(refilled.wait(),2)
            state=lib.read_json(manifest)
            state["documents"][doc]=rows[doc]
            lib.save(manifest,state)
            return {"ok":True,"data":{"status":"published"}}

        return await runner._rolling_pass(tmp_path,list(rows.values()),call)

    failures=asyncio.run(scenario())
    assert starts==[("a","publish"),("b","prepare"),("c","prepare"),("c","publish")]
    assert [(item["doc_id"],item["code"]) for item in failures]==[("b",lib.PREPARE_WORKER_EXITED)]
    assert set(lib.read_json(manifest)["documents"])=={"a","c"}


def test_rolling_pauses_before_refill_and_resumes_cached_preparation(tmp_path, monkeypatch):
    a,b,c=[{"id":x,"sha256":x} for x in "abc"]
    manifest=tmp_path/"manifest.json"
    pause=tmp_path/"pause.json"
    lib.save(manifest,{"documents":{}})
    monkeypatch.setattr(runner,"current_windows",lambda _: {})
    launches=[]

    async def interrupted(_,args):
        doc,phase=args["doc_id"],args["phase"]
        launches.append((doc,phase))
        if doc=="a":
            lib.save(manifest,{"documents":{"a":a}})
            lib.save(pause,{"requested":lib.now()})
            return {"ok":True,"data":{"status":"published"}}
        assert (doc,phase)==("b","prepare")
        lib.save(tmp_path/"documents/b/lane-prepare.json",{"status":"prepared","sha256":"b"})
        return {"ok":True,"data":{"status":"prepared"}}

    with pytest.raises(lib.PauseRequested):
        asyncio.run(runner.ingest_rolling(tmp_path,[a,b,c],interrupted,True))
    assert launches==[("a","publish"),("b","prepare")]
    pause.unlink()
    launches.clear()

    async def resumed(_,args):
        doc,phase=args["doc_id"],args["phase"]
        launches.append((doc,phase))
        if phase=="publish":
            state=lib.read_json(manifest)
            state["documents"][doc]={"sha256":doc}
            lib.save(manifest,state)
        return {"ok":True,"data":{"status":"prepared" if phase=="prepare" else "published"}}

    assert asyncio.run(runner.ingest_rolling(tmp_path,[a,b,c],resumed,True))==(0,[])
    assert launches==[("b","publish"),("c","prepare"),("c","publish")]
    assert set(lib.read_json(manifest)["documents"])=={"a","b","c"}


def test_rolling_quota_wait_retries_remaining_with_prepared_checkpoints(tmp_path, monkeypatch):
    a,b=[{"id":x,"sha256":x} for x in "ab"]
    manifest=tmp_path/"manifest.json"
    lib.save(manifest,{"documents":{}})
    windows=iter([{},{"codex":{"primary":{"usedPercent":100,"resetsAt":10**10}}},{}])
    monkeypatch.setattr(runner,"current_windows",lambda _:next(windows))
    waits=[]
    calls=[]

    async def wait(root,until,doc):
        waits.append(doc)

    async def call(_,args):
        doc,phase=args["doc_id"],args["phase"]
        calls.append((doc,phase))
        if phase=="prepare":
            lib.save(tmp_path/"documents/b/lane-prepare.json",{"status":"prepared","sha256":"b"})
            return {"ok":True,"data":{"status":"prepared"}}
        if doc=="a" and calls.count(("a","publish"))==1:
            lib.save(tmp_path/"documents/a/lane-publish.json",
                     {"sha256":"a","started":lib.now(),"error":"usage limit"})
            return {"ok":False,"error":{"code":"native_ingestion_failed"}}
        state=lib.read_json(manifest)
        state["documents"][doc]={"sha256":doc}
        lib.save(manifest,state)
        return {"ok":True,"data":{"status":"published"}}

    monkeypatch.setattr(runner,"wait_for_window",wait)
    assert asyncio.run(runner.ingest_rolling(tmp_path,[a,b],call,True))==(0,[])
    assert waits==["a"]
    assert calls==[("a","publish"),("b","prepare"),("b","publish"),("a","prepare"),("a","publish")]
