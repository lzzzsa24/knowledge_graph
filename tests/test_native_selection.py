import asyncio
import psutil
import pytest

from kg_pilot import native_library as lib, native_readiness
from scripts import run_native_library as runner


def test_pause_exception_is_bound_to_runner_revision_and_pause_request(tmp_path):
    row={"id":"first","sha256":"verified"}
    lib.save(tmp_path/"pause.json",{"requested":"original"})
    proc=psutil.Process()
    lib.save(tmp_path/"selected-run.json",{"pid":proc.pid,"created":proc.create_time(),"run_id":"trial",
        "documents":{"first":"verified"},"pause_sha256":lib.file_hash(tmp_path/"pause.json")})
    lib.check_pause(tmp_path,selection="trial",row=row)
    for token,doc in [(None,row),("old",row),("trial",dict(row,id="other")),("trial",dict(row,sha256="new"))]:
        with pytest.raises(lib.PauseRequested):
            lib.check_pause(tmp_path,selection=token,row=doc)
    lib.save(tmp_path/"pause.json",{"requested":"user paused again"})
    with pytest.raises(lib.PauseRequested):
        lib.check_pause(tmp_path,selection="trial",row=row)


def test_selected_pass_never_wakes_other_documents_or_changes_queue_limit(tmp_path,monkeypatch):
    root=lib.directory(tmp_path)
    docs=[dict(id=k,sha256=k*64,group=1,prebuilt=False) for k in "abc"]
    lib.save(root/"queue.json",{"documents":docs})
    lib.save(root/"manifest.json",{"documents":{}})
    lib.save(root/"queue_settings.json",{"documents":4})
    lib.save(root/"pause.json",{"requested":"keep"})
    monkeypatch.setattr(runner,"verify_source",lambda *_:True)
    calls=[]
    async def call(name,args):
        assert name=="native_library_ingest_workspace"
        selected,=args["documents"]
        k=selected["id"]
        lib.check_pause(root,selection=args["selection"],row=next(r for r in docs if r["id"]==k))
        calls.append((args["mode"], k))
        if args["mode"] == "preflight":
            job = root / "documents" / k
            for name in ("crop-review.json", "mixed-parse.json"):
                lib.save(job / name, {})
            lib.save(job / "preflight.json", {"policy":native_readiness.POLICY, "status":"ready",
                "sha256":selected["sha256"], "inputs":{
                    name:lib.file_hash(job / name) for name in ("crop-review.json", "mixed-parse.json")}})
            return {"ok":True,"data":{"ready":[k]}}
        state=lib.read_json(root/"manifest.json")
        state["documents"][k]={"sha256":selected["sha256"]}
        lib.save(root/"manifest.json",state)
        return {"ok":True,"data":{"status":"published"}}
    assert asyncio.run(runner.run_selection(tmp_path,["a@"+docs[0]["sha256"]],1,call))==0
    assert calls==[("preflight","a"),("ready","a")] and lib.document_concurrency(root)==4 and (root/"pause.json").is_file()
    assert not (root/"selected-run.json").exists()
    with pytest.raises(ValueError):
        runner.selected_rows(tmp_path,["b@wrong"])


def test_two_selected_lanes_share_one_writer_and_keep_other_rows_paused(tmp_path):
    rows=[{"id":k,"sha256":k*64} for k in "abc"]
    lib.save(tmp_path/"pause.json",{"requested":"original"})
    lib.save(tmp_path/"manifest.json",{"documents":{}})
    proc=psutil.Process()
    lib.save(tmp_path/"selected-run.json",{"run_id":"batch","pid":proc.pid,"created":proc.create_time(),
            "pause_sha256":lib.file_hash(tmp_path/"pause.json"),"documents":{r["id"]:r["sha256"] for r in rows}})
    active=set()
    writers=set()
    peaks=[]
    async def call(name,args):
        k=args["doc_id"]
        assert args["selection"]=="batch" and k in "abc"
        active.add(k)
        if args["phase"]=="publish":
            writers.add(k)
        peaks.append((len(active),len(writers)))
        await asyncio.sleep(.02)
        if args["phase"]=="publish":
            manifest=lib.read_json(tmp_path/"manifest.json")
            manifest["documents"][k]={"sha256":args["sha256"]}
            lib.save(tmp_path/"manifest.json",manifest)
            writers.remove(k)
        active.remove(k)
        return {"data":{"status":"prepared" if args["phase"]=="prepare" else "published"}}
    assert asyncio.run(runner._rolling_pass(tmp_path,rows,call,selection="batch",concurrency=2))==[]
    assert max(n for n,_ in peaks)==2 and max(w for _,w in peaks)==1
    assert (tmp_path/"pause.json").is_file()


def test_crops_use_free_slot_during_publication_and_never_publish(tmp_path,monkeypatch):
    root=lib.directory(tmp_path)
    rows=[dict(id=k,sha256=k*64,group=1,prebuilt=False) for k in "abc"]
    lib.save(root/"queue.json",{"documents":rows})
    lib.save(root/"manifest.json",{"documents":{}})
    lib.save(root/"pause.json",{"requested":"keep"})
    proc=psutil.Process()
    lib.save(root/"selected-run.json",{"run_id":"publishing","pid":proc.pid,"created":proc.create_time(),
             "started":"2026","concurrency":1,"documents":{"a":rows[0]["sha256"]},
             "pause_sha256":lib.file_hash(root/"pause.json")})
    monkeypatch.setattr(runner,"verify_source",lambda *_:True)
    papers=[]
    async def review(name,args):
        assert name=="native_library_run_lane" and args["phase"]=="crops"
        assert lib.live_selection(root,"crop-run.json")["concurrency"]==1
        row=next(r for r in rows if r["id"]==args["doc_id"])
        lib.check_pause(root,selection=args["selection"],row=row)
        with pytest.raises(lib.PauseRequested):
            lib.check_pause(root,selection=args["selection"],row=rows[0])
        papers.append(row["id"])
        return {"ok":True,"data":{"status":"needs_review"}}
    assert asyncio.run(runner.run_selection(tmp_path,["b","c"],2,review,crops_only=True))==0
    assert papers==["b","c"] and lib.read_json(root/"manifest.json")["documents"]=={}
    assert (root/"pause.json").exists() and not (root/"crop-run.json").exists()
