"""Project knowledge_ingestion MCP operations for migration of catalogued originals."""
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
import os
import sys

from kg_pilot import native_library as lib
from kg_pilot.native_pair import _run_lane, run_queue_pair, run_queue_group
from kg_pilot.mcp_ingestion import IngestionTools, MCPToolError, _spec


def _worker_output_to_stderr():
    # The MCP parent owns stdout for protocol frames, including at the OS level.
    os.dup2(sys.stderr.fileno(), 1)
    sys.stdout = sys.stderr


def native_specs():
    return [
        _spec("native_library_status","查询历史 PDF 原生图文入库队列及发布进度。",{},read_only=True),
        _spec("native_library_prepare","验证最近提交的原生索引快照，必要时回退至已核验的上一快照。",{}),
        _spec("native_library_search","在已发布的 RAG-Anything 原生块向量中本地检索，返回对应原 PDF 与物理页。",
              {"query":{"type":"string","minLength":1,"maxLength":2000}},("query",),read_only=True),
        _spec("native_library_ingest","以已登录的 GPT-6.2 Sol High 订阅为指定的现有文献建原生图文索引，完成后校验并发布。",
              {"doc_id":{"type":"string","pattern":"^[A-Za-z0-9_.-]{1,140}$"},"single_only":{"type":"boolean"}},("doc_id",)),
        _spec("native_library_ingest_pair","并行处理指定的两份历史原始 PDF，并按顺序校验和发布到同一知识库。",
              {"doc_ids":{"type":"array","minItems":2,"maxItems":2,"uniqueItems":True,
                          "items":{"type":"string","pattern":"^[A-Za-z0-9_.-]{1,140}$"}}},("doc_ids",)),
        _spec("native_library_ingest_group","并行处理一组历史 PDF，依次校验并发布。",
              {"doc_ids":{"type":"array","minItems":3,"maxItems":lib.MAX_DOCUMENT_CONCURRENCY,"uniqueItems":True,
                          "items":{"type":"string","pattern":"^[A-Za-z0-9_.-]{1,140}$"}}},("doc_ids",)),
        _spec("native_library_ingest_workspace","在同一个 RAG-Anything 工作区并行建索引，核验后提交这一批。",
              {"documents":{"type":"array","minItems":1,"maxItems":lib.MAX_DOCUMENT_CONCURRENCY,"uniqueItems":True,
                            "items":{"type":"object","additionalProperties":False,"required":["id","sha256"],
                                     "properties":{"id":{"type":"string","pattern":"^[A-Za-z0-9_.-]{1,140}$"},
                                                   "sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"}}}},
               "selection":{"type":"string","pattern":"^[a-f0-9]{32}$"},
               "mode":{"type":"string","enum":["preflight","ready"]}},("documents",)),
        _spec("native_library_run_lane","对队列中的指定 PDF 修订执行隔离构建或正式发布。",
              {"doc_id":{"type":"string","pattern":"^[A-Za-z0-9_.-]{1,140}$"},
               "sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
               "selection":{"type":"string","pattern":"^[a-f0-9]{32}$"},
               "phase":{"type":"string","enum":["crops","prepare","publish"]}},("doc_id","sha256","phase")),
    ]


class NativeTools(IngestionTools):
    def dispatch_tool(self,name,arguments):
        if name=="native_library_status":
            return lib.state(self.pilot)
        if name=="native_library_search":
            return lib.search(self.pilot,arguments["query"])
        if name=="native_library_prepare":
            with lib.file_lock(lib.directory(self.pilot)/"writer.lock"):
                verified=lib.initialize(self.pilot)
                return {"generation":verified["generation"],"published":len(verified["documents"])}
        if name in ("native_library_ingest","native_library_ingest_pair","native_library_ingest_group",
                    "native_library_run_lane","native_library_ingest_workspace"):
            try:
                # LightRAG keeps process-global locks bound to the first document's event loop.
                with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"),
                                         initializer=_worker_output_to_stderr) as worker:
                    if name=="native_library_ingest_workspace":
                        from kg_pilot.native_workspace import publish_workspace
                        return worker.submit(publish_workspace,self.pilot,arguments["documents"],
                                             arguments.get("selection"),arguments.get("mode","ready")).result()
                    if name=="native_library_run_lane":
                        root=lib.directory(self.pilot)
                        plan=lib.read_json(root/"queue.json")["documents"]
                        row=next((r for r in plan if r["id"]==arguments["doc_id"]),None)
                        permitted=root/"marked_revisions.json"
                        approved=lib.read_json(permitted) if permitted.is_file() else {}
                        if (not row or row["prebuilt"] or row["sha256"]!=arguments["sha256"]
                                or not lib.eligible(row,approved)):
                            raise ValueError("PDF revision is outside the authorized queue")
                        selection=arguments.get("selection")
                        lib.check_pause(root,selection=selection,row=row)
                        if selection:
                            filename="crop-run.json" if arguments["phase"]=="crops" else "selected-run.json"
                            if lib.live_selection(root,filename).get("run_id")!=selection:
                                raise ValueError("The requested operation differs from the selected run")
                        return worker.submit(_run_lane,self.pilot,row,arguments["phase"],selection).result()
                    if name=="native_library_ingest_pair":
                        return worker.submit(run_queue_pair, self.pilot, arguments["doc_ids"]).result()
                    if name=="native_library_ingest_group":
                        return worker.submit(run_queue_group, self.pilot, arguments["doc_ids"]).result()
                    handler=lib.publish_document if arguments.get("single_only") else lib.run_document
                    return worker.submit(handler, self.pilot, arguments["doc_id"]).result()
            except Exception as error:
                if (isinstance(error, BrokenProcessPool) and name=="native_library_run_lane"
                        and arguments["phase"]=="prepare"):
                    raise MCPToolError(lib.PREPARE_WORKER_EXITED, "文献的隔离构建进程提前退出。") from error
                raise MCPToolError("native_ingestion_failed","原生入库失败；详情及已完成的检查点保存在本机。") from error
        return super().dispatch_tool(name,arguments)
