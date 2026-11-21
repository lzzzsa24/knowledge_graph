# 混合键合知识图谱：文献与行业资料库

截至 2026-11-07，正式库有 **213 份资料、2,765 页、1,439 条知识记录**。最新一轮由 Codex 分五组净增 100 份全文，覆盖期刊与会议论文、技术演示、欧洲专利、企业设备与工艺资料以及计量指南。可查看[新增 100 份目录与原件](pilot/reports/collection_20261106_hundred/目录.md)、[逐份发布与引用核验](pilot/reports/collection_20261106_hundred/publication_audit.json)和[中文检索与问答验收](pilot/reports/collection_20261106_hundred/验收.md)。

上一轮将资料库扩至 113 份，新增 6 篇研究论文、2 份厂商资料、3 份技术路线图和 2 份 IPC 测试方法。参见[上轮资料目录](pilot/reports/collection_20261106_parallel/目录.md)和[上轮中文检索复测](pilot/reports/collection_20261106_parallel/retrieval_check.json)。

本项目从零收集公开文献，不继承学长的文献库、代码、图谱或题库，参考 PPT 仅用于理解目标。原始试点于 2026-11-02 完成 **26 篇、265 页、128 条知识记录**。2026-11-03 已扩充至 **100 份资料、1,538 页、566 条知识记录**，包括论文、行业技术资料、标准与计量指南。打开[工艺与文献](http://127.0.0.1:8600/#/papers)浏览，或查看[本轮完整目录与统计](pilot/reports/expansion_20261103/summary_100.json)、[最终数据审计](pilot/reports/expansion_20261103/audit_100.json)。

原始[试点报告与技术选择](pilot/reports/pilot_report.md)、[26 篇文献目录](pilot/catalog/literature_catalog.md)保留为历史记录。可执行基线为 PyMuPDF 正文、原试点 MinerU 定量表格与 Docling/RapidOCR 乱码恢复，Pydantic/Pint 校验，SQLite/NetworkX 保存证据与关系，BGE-M3/Qdrant 检索，FastAPI Web 与 Streamlit 展示。批量入口使用 PyMuPDF/本地 RapidOCR 及来源复核。LightRAG/RAG-Anything 的 18 条文字路径验证仍属独立历史试验，未作为全语料生成服务。

## 版本管理

Git 跟踪源码、测试、启动脚本、项目文档和选定的研究规则。原始 PDF、文献目录快照、解析产物、索引、入库检查点、模型缓存及本机模型接入配置由 `.gitignore` 保留在本地。新工作区需要单独恢复这些数据，依赖完整语料的测试和部分历史验收链接也需要对应资料。入库检查点与恢复方法见[入库与断点恢复](pilot/framework/native_library/入库与断点恢复.md)。

## 打开本地界面

在资源管理器中双击项目根目录的 [启动知识库.cmd](启动知识库.cmd)，待窗口显示 `Uvicorn running on http://127.0.0.1:8600` 后，打开[研究地图](http://127.0.0.1:8600/#/graph?view=network)或[论文问答](http://127.0.0.1:8600/#/ask)。使用期间保持启动窗口打开；关闭系统时按 `Ctrl+C`，若询问是否终止批处理，输入 `Y`。关闭浏览器只关闭页面，服务仍会运行。重启电脑后需要重新启动。

此入口使用 Windows 自带的 Windows PowerShell 调用项目环境，无需管理员权限，也不经过 PowerShell 7 的交互启动更新提示。若遇到端口已占用，请先检查是否已有启动窗口，避免重复启动。

主界面为[本地 Web 资料库](http://127.0.0.1:8600/#/papers)，已实际核对 100 份资料、工艺入口及组合筛选。另保留 [Streamlit 浏览界面](http://127.0.0.1:8501)，如需启动，在本项目 PowerShell 中执行：

```powershell
.\scripts\start_pilot.ps1
```

在“全文检索”直接输入中文问题即可。“自动”模式会为中文和中英混输的问题匹配英文领域术语，结合原文关键词和本地 BGE-M3 语义检索排序。例如，输入“硅通孔的直径和间距”，可以找到使用 TSV、diameter、pitch 的英文原段，并打开对应的 PDF 物理页。首次载入本地语义模型需要稍候。纯英文查询默认使用原文关键词，也可手动选择语义检索。

[100 份资料的中文检索验证报告](pilot/reports/crosslingual_20261106/comparison.md) 给出了改进前后命中同一篇资料、同一 PDF 页和同一原文段落的结果，并列出仍会漏检的问法。

## 论文问答与模型 API 配置

### 使用同一入库模型构建两种索引

`scripts/ingest_shared.py` 使用“批量入库”的并行智算云连接。一批任务固定使用指定模型抽取、复核，再将已发布的逐页正文和带来源的表格交给 RAG-Anything 建图。向量使用本机 BGE-M3。页码随内容传入，图片不上传。正式库和框架索引分别在校验完成后发布。

在工作区运行 `.\.venv\Scripts\python.exe scripts/ingest_shared.py --help` 查看参数。首次指定 PDF、独立的 `--run-dir`、入库配置中选定的 `--model` 和本次 `--token-limit`。增加 `--baseline-only` 即可只建立现有系统的索引。使用同一运行目录、省略 PDF 和该选项可以从检查点继续并补建图索引。入库和复核沿用原有的正式发布流程。

`run.json` 记录批次、各阶段耗时和正式库的存储增量，`calls.jsonl` 逐次保存服务端报告的用量。只有整批文献处理完成、检索块的页码核对成功后，框架索引才发布在 `pilot/framework/shared/<批次>/<输入指纹>/`。沿用该运行目录、模型和预算，增加 `--find "问题"` 可以查询该批图索引并返回原始 PDF 路径与物理页。网页现有的 Gao / Adeia 选项仍指向此前单独验证过的两篇索引。

论文问答的“检索方式”可在“全库检索”和“全库检索＋Gao / Adeia 框架原页”之间切换。后者先运行相同的全库检索，再查询两篇文献已经建立的 RAG-Anything 图索引。每个原生检索块按文献版本回连 PDF 的物理页。回答可以使用同页表格，相关的表格、字形或图像证据每次最多附 6 张原页图，仅向此前授权的并行智算云地址发送这两份原件的页图。点击句后的来源可核对匹配过的短引并打开实际页。每轮框架回答下方显示补充原页数、实际发送的图片数及服务商返回的 token 用量。

需要限定来源时，可用完整题名写成“仅依据《题名一》和《题名二》回答：问题”。系统会先按题名核对身份，再把候选来源限定在指定文献内。普通提问仍可检索整库。原 PDF 换版、框架索引缺失或模型地址不适用时，页面会说明回退原因。

两篇索引保存在 `pilot/framework/gao_adeia/`，独立于资料入库时整体切换的正式索引。需要从原有试验资产重新安装时，运行 `.\.venv\Scripts\python.exe scripts/promote_framework_index.py`。脚本核对 PDF 和文件指纹并复制已生成的检索数据，不调用模型；发现已安装的数据不一致时会停止。

推荐打开 [论文问答](http://127.0.0.1:8600/#/ask)。若服务已关闭，运行 `.\scripts\start_web.ps1`。

1. 在“模型 API 配置”选择并行智算云预设，或选择自定义的 OpenAI 兼容服务。自定义时填写服务商给出的 API 基础地址（含版本路径）、账号可调用的聊天模型 ID 和 API 密钥。例如 [DeepSeek](https://api-docs.deepseek.com/) 和[阿里云百炼](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) 都提供这样的入口。远端地址使用 HTTPS，本机服务也可以使用回环 HTTP。
2. 点击“连接并保存”。手填自定义模型 ID 时，后端用简短测试消息验证连接，测试消息不含论文或对话。服务商支持模型列表时也可留空模型 ID，尝试自动选择。原有并行配置可继续使用；换地址时需输入该地址的密钥。
3. 输入论文问题。中文问题与全文检索共用中英文术语匹配和原文排序。问答还会补充有关知识记录、实验条件和结果表，再将选中的原文片段、问题和最近对话发到所选服务的 `/chat/completions`。术语匹配在本机完成，无需额外调用翻译 API。
4. 点击 `[S1]` 等引用查看原引文和 PDF 实际页。证据不足会明确说明；检查到不存在的引用编号、无引用的肯定回答、输出截断或服务错误时，不展示不完整回答。

系统要求模型生成 JSON 并在本地校验；只有回答格式/必填字段异常时，才使用同一份证据自动重试一次（一次提问最多两次模型生成）。合法的完整 JSON 围栏或 BOM 可规范化；普通文字不会绕过引用校验。不存在的引用、缺引用的肯定回答、输出截断和 HTTP 错误不触发格式重试。`pilot/runs/qa_events.jsonl` 只记录格式类型、长度、位置、次数等诊断元数据，不记录问题、回答、内部思维或密钥。

密钥使用 Windows 当前用户 DPAPI 加密。保存新连接时，密文和对应的地址、模型一起写入 `pilot/llm_config.json`，旧版 `pilot/secrets/` 中的并行平台密钥仍可读取。通用服务也可以通过环境变量 `KG_LLM_BASE_URL`、`KG_LLM_API_KEY`、`KG_LLM_MODEL` 配置；原有并行平台变量 `PARATERA_API_KEY` 和 `PARATERA_MODEL` 继续生效。服务按地址使用密钥；浏览器不将密钥写入本地存储。

Web 会话只保留本页最近 6 轮，刷新即清空。问答列表默认“最新在前”，可切换“最早在前”；轮次编号持续递增，模型接收的历史始终按时间顺序。Streamlit 同样支持按完整问答轮次切换排序。

回答直接解释概念或结论，句末保留来源引用；只有缺失条件、数据冲突或无法支持的推断才说明具体限制，不例行复述“按本次证据”等套话。页面普通回答不重复显示“证据支持”标签。

问答同时只执行一个生成请求。前端清空或超时会停止等待，不保证取消平台已经接收的请求。模型给出的“支持/部分支持/不足”是其证据判断，不是事实准确率或专家确认；编号存在检查也不能证明所有语义引用都正确。完整回答有合法引用但模型漏掉辅助判断字段时，内部记为 `unassessed` 并正常展示，不推断其充分性；未知引用或缺少引用的肯定回答仍会被阻止。

该问答功能直接使用现有本地检索与证据链，没有将完整语料重新导入 LightRAG，也没有把历史的 agent 桥接试验伪装成在线问答。真实模型联调与单元测试的状态分开记录。

## 创新点生成

打开 [创新点生成](http://127.0.0.1:8600/#/ideas)，使用论文问答当前保存的服务、模型和密钥。

- **随机探索**：从资料库内随机选择研究线索，检索相关原文后生成候选研究设想。
- **按输入生成**：输入关键词、研究方向、实验条件或粘贴讨论；也可将当前页面会话中的问答导入编辑框，检查和补充后再生成。
- 页面可选每次生成 1、3 或 5 个候选，分别展示研究问题、研究假设、带引用的文献依据、可能创新之处、验证步骤、风险与查重检索词。点击来源可核对 PDF 页；候选可复制或放回输入框继续完善。

生成的是待验证的研究设想。原文事实与假设分开呈现；创新性尚未经过全领域文献或专利查重，检索词用于后续核实。当前模块使用本地资料检索，不自动联网查新。结果只保存在当前页面会话中，切换页面保留，刷新会清空；不会自动写入知识图谱或改变原有资料。

实现位于 `src/kg_pilot/innovation.py`，接口为 `POST /api/ideas`。与问答共享生成锁和已有模型配置；检索会补充命中块的同页邻段，原文逐块保留独立来源。格式修复最多额外请求一次。真实联调脚本为 `scripts/check_innovation.py`，报告保存在 `pilot/reports/innovation_20261105/`，与 mock 单元测试分开记录。

## 程序化调试

项目还提供 [agent 协作 MCP](docs/agent-collaboration.md)，供已授权协作的 Codex 与 Claude Code 共享消息、交接结果和认领文件范围。服务名为 `agent_collaboration`，两端使用各自身份并共享本地 SQLite；消息保留到重连后，不依赖 Web 服务。完整工具、配置与验证方式见说明文档。

日常排查优先使用接口与脚本，浏览器用于最后验证布局、点击和原页显示。接口定义可查看 [本机 API 文档](http://127.0.0.1:8600/api/docs)。以下命令在项目目录运行：

```powershell
# 服务、语料数量与索引文件状态，不请求生成模型
.\.venv\Scripts\python.exe scripts/diagnose_ideas.py status

# 查看检索命中及实际发给模型的原文上下文，不消耗生成 API 额度
.\.venv\Scripts\python.exe scripts/diagnose_ideas.py preview --prompt "低温混合键合与表面活化" --count 1

# 生成期间也可查看进度、阶段、耗时、来源编号与错误码
.\.venv\Scripts\python.exe scripts/diagnose_ideas.py runs

# 真实调用已配置模型：留空随机，或提供 --prompt / --input-file
.\.venv\Scripts\python.exe scripts/diagnose_ideas.py generate --count 1
```

`preview` 与实际生成共用证据准备逻辑；随机预览和随后独立发起的随机生成会重新抽样。可以使用 `--history-file` 提供对话 JSON。详细 JSON 默认保存到 `.cache/idea_diagnostics/`，可用 `--output` 指定路径，终端只输出摘要与报告位置。显式导出的报告包含该次输入与检索原文；不要将私人输入的报告随意公开。

对应接口为 `GET /api/debug/health`、`POST /api/debug/ideas/preview`、`GET /api/debug/ideas/runs` 和 `POST /api/ideas`，均限本机请求。运行记录只保留最近 30 条脱敏元数据于服务进程内存，不保存提问、对话、原文、模型正文或密钥，重启清空。健康检查和运行状态在模型生成期间仍可读取；健康检查是存活与文件存在性检查，不替代完整数据审计。前端改动刷新页面生效，Python 后端改动需要重启服务。

2026-11-05 验证：373 项 Python 回归通过、1 项可选检查跳过，前端会话控制器测试通过。真实调用覆盖随机、方向与对话输入；最终通过命令行生成 3 个候选，在生成过程中用约 0.61 秒读到运行阶段，并确认预览与定向生成使用相同来源。浏览器检查了结果呈现、跨页保留、继续完善与原页引用。测试记录和已发现的内容局限见 [本轮验证记录](pilot/reports/innovation_20261105/validation.json)；这不代表候选已具备科学创新性或全部论断正确。

## Web 前端（知识图谱界面）

`web/` 是与 Streamlit 界面并行的前端：FastAPI 提供 `/api/*`（`web/server.py`），`web/static/` 为无需构建的 HTML/JS 页面，不依赖外部 CDN。页面包括论文问答、概览、研究地图与证据关系图、知识记录筛选、文献库、关键词/语义检索、批量入库与框架试验；每条记录都可打开原文引文与 PDF 实际页图。浏览和问答读取已发布资料，批量入口负责校验后新增资料，模型配置在本机保存。

```powershell
.\scripts\start_web.ps1   # http://127.0.0.1:8600 ，接口文档见 /api/docs
```

本地 Qdrant 存储一次只允许一个客户端：避免与 Streamlit 界面或建库命令同时执行语义检索。

### 怎样使用研究地图

从左栏进入[研究网络](http://127.0.0.1:8600/#/graph?view=network)，可按主要工艺浏览文献点，沿连线核对引用、跨研究对照和同组数据的重复报告；菱形可打开经过查新的具体研究问题。选中文献后可打开仅含本篇样品与记录的点图，或展开全库。

要核对研究覆盖，打开[证据矩阵](http://127.0.0.1:8600/#/graph?view=matrix)。先看关心的工艺与结果，在「原创实测」和「实测对照」之间切换。前者回答哪些试样测过这个结果，后者要求原研究改变工艺并进行了比较。点击数字可查样品、测量口径及 PDF 原页。遇到证据稀少的格子，「到原文查这个交叉」可找同一段内同时提及工艺与结果的文献。这些待核线索单独展示。

「研究脉络」可沿论文书目打开它引用的研究，也可比较同材料、同工艺下的两个观测。「研究对照」收录按原文核对过的专题，例如五层整叠无空洞与中心链电通过的不同分母。「潜在创新方向」列出待检验的问题、最接近的既有工作、实验对照与判定办法。查阅与复制均在本机完成，送入创新点生成页后可先编辑草稿。

程序化检查可从只读接口 `/api/research-network`、`/api/research-map`、`/api/research-citations`、`/api/research-notes` 和 `/api/research-text-leads` 取得与页面相同的依据。矩阵可按 `route`、`system` 筛选，正文补查另需 `row` 和 `column`。新增资料进入文献目录和全文索引后，跨论文复核与候选方向的查新分别更新；原件或对应知识记录变更时，旧的署名连线须重新核对。

## 工艺分类与多维筛选

2026-11-03 已按确认的标准完成现有 26 篇论文的文献级分类。打开 [工艺与文献](http://127.0.0.1:8600/#/papers)：六个工艺阶段分别为结构制备、平坦化、清洗活化、对准接触、键合后退火、键合后加工；另有 D2W 芯片制备搬运、检测表征与性能评价、全流程与综合研究三个配套入口。每篇只有一个主归属，支持“包含关联阶段”浏览，同一入口内按文献去重。Streamlit 的文献库也支持相同分类与筛选。

可组合筛选课题相关性、技术路线、材料体系与界面材料、关注问题、研究方法、资料形式、内容性质、发表状态、获取途径；同一维度多选取并集，不同维度取交集，并可叠加年份与关键词。Web 页面会保留筛选链接及页面返回状态。入口数字表示全库文献数，标签数字按其余当前筛选条件计算。资料不足以确定路线时显示“未明确”。多维筛选默认折叠，避免遮挡文献列表。

26 篇中 24 篇归为核心研究，2 篇为专项支撑（纯 SiCN 制备演示、纯 SiO2 强度测量）；相关性不表示可信度。原始研究的路线标签以实际研究对象为准，综述标签表示其主要覆盖范围。材料标签包括主体系及实际研究的关键界面材料，不代表所有材料均直接互相键合。关注问题归并为 21 个浏览类别，精确的文内细词另外保留；“电连接与电阻”等归并标签不改变接触电阻、总连接电阻等测量定义。

点击论文可查看分类理由、标签及对应 PDF 物理原页。74 处分类依据引文已通过原文匹配和页码检查；这是 agent 分类与来源定位校验，不是专家审核。分类记录保存在 `pilot/catalog/classifications.json`，标准词表和来源输入保存在同目录；原始 `papers.json`、PDF 和 128 条知识记录及其旧工序字段保持原样，文献标签不会批量覆盖论断性质。

分类重建命令为 `.\.venv\Scripts\python.exe scripts\build_classification.py`。脚本校验覆盖范围、词表、引文页码和 PDF 哈希后才发布分类数据及 `pilot/reports/classification_validation.json`。它只重建已审核输入，不自动为新论文分类。目录记录的 PDF 哈希与分类记录不一致时，界面不沿用旧标签；实际文件哈希在重建时核验，新增资料可进入待分类状态。

原始资料、证据片段和知识记录沿用现有追溯关系；本轮没有新生成跨论文专题综述，也没有把试点抽取改称全文穷尽抽取。

## 批量入库

Codex 的 `ingestion_start` 可指定 `workers=1` 至 `10`（默认 1）。多份资料可并行抽取及复核，单份资料先抽取再复核。恢复同一批次沿用原并发数，PDF 解析和最终发布各自顺序执行。

两种模式都会在抽取知识点前逐页检查文字编码。对能与原 PDF 同一位置、同一字体的字形对应的字符，系统保留原始提取文本，并用修正文本生成引文和检索索引。无法确认的符号连同物理页码记录下来，依赖这些符号的定量知识点需要复核。问答遇到仍待核实的符号时会收到明确标记。

如需按当前规则复查历史资料，先运行 `.\.venv\Scripts\python.exe scripts/backfill_text_quality.py preview --all`。可用 `--doc-id reliability_gao_d2w_2021` 代替 `--all` 只查一份。将 `preview` 改为 `apply` 会通过暂存、校验和备份流程更新资料及索引。正式复查报告位于 `pilot/reports/text_quality_backfill.json`。

入库会为每份资料生成 `pilot/parsed/<doc_id>/fulltext.md`。在“工艺与文献”打开资料，选择“阅读 Markdown”，或直接用编辑器打开文件。阅读版按 PDF 物理页分节，利用原文段落边界合并换行，并根据原 PDF 字体还原可确认的特殊符号。遇到无法解码的符号或公式，可点击旁边的链接查看原页。可定位的表格随正文排放，行列仍待核验的旧抽取单独标注。历史 TXT 保留供查阅。若需从已有解析重新生成阅读版，可运行下面的命令，末尾加 `--check` 则仅检查文件是否与当前数据一致。

```powershell
.\.venv\Scripts\python.exe scripts/build_reading_markdown.py
```

**通过 Codex 入库**　在本项目的 Codex 对话中提供本地 PDF 路径，例如「把 `F:\papers\sample.pdf` 加入知识库，用 Codex 抽取并复核，完成后告诉我入库结果」。Codex 通过项目级 MCP 服务 `knowledge_ingestion` 提交资料、查看批次进度并获取发布结果。若希望先看抽取结果，可以明确要求「先暂存，等我确认后再发布」。已中断的批次可凭批次编号继续。服务通过标准输入输出运行，无需打开网页。

MCP 注册在 `.codex/config.toml`，由项目虚拟环境启动 `scripts/ingestion_mcp.py`。在项目目录执行 `codex mcp get knowledge_ingestion` 可检查注册。实际抽取和复核各自使用已登录的本机 Codex CLI，以 `gpt-6.1-sol` 和 `ultra` 在独立上下文执行。提交会立即返回批次编号；Codex 通过 `ingestion_wait` 等待最终结果。Windows 客户端关闭连接时可能终止后台进程，再次连接后通过 `ingestion_status` 和 `ingestion_resume` 查询并继续同一批次。[OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

**通过模型 API 入库**　打开[批量入库页面](http://127.0.0.1:8600/#/ingestion)，选择 PDF 并开始处理。可以复用问答连接的 API 地址和密钥，单独指定入库模型，也可以填写独立配置。问答地址变更后需重新确认复用。配置保存在 `pilot/ingestion_llm_config.json`。通用环境变量为 `KG_INGEST_LLM_BASE_URL`、`KG_INGEST_LLM_API_KEY` 和 `KG_INGEST_LLM_MODEL`，旧的并行平台环境变量继续生效。

两种方式共用去重、逐页解析、知识抽取、复核、索引构建及发布流程。每批最多 20 份 PDF，单份最大 30 MB，合计最大 60 MB，每份最多 120 页。默认每份重点抽取最多 8 条知识，可设置 1–30 条，完整可解析正文进入检索。

每次生成经过严格 JSON、受控词表与页码引文校验，格式问题最多自动重试一次。复核拒绝分类或知识时，允许根据明确意见重新抽取一次，再用新的独立上下文复核；仍未通过则进入待处理。自动复核不是专家审核。模型未收到的图像内容不作为已理解的证据。

空白页保留编号；扫描件或明显乱码尝试本地 RapidOCR，保留原文字层及 OCR 标记。有内容但无法可靠解析的页面会停止该资料处理。相同哈希的文件自动跳过；同 DOI/标题的不同文件进入版本核对，不自动覆盖旧资料。当前接收 PDF，尚不支持 PPT、网页、Word 直接上传或自动爬取链接。

两种入库模式都会尝试读取 PDF 中行列清晰的表格，保存表题、原始行标签、数值、单元格位置和物理页码。问答命中一项实验结果时，可一起引用同页的相关表格或检测方法段。历史资料可按文献与物理页补表。以下命令预览 Gao 论文第 3 页，核对后可将 `preview` 改为 `apply`，通过原有发布与备份流程更新检索索引。

```powershell
.\.venv\Scripts\python.exe scripts/backfill_table_evidence.py preview --doc-id reliability_gao_d2w_2021 --pages 3
```

在终端维护批次时，也可以使用已有命令行工具。

```powershell
# 目录参数读取其直接子级 PDF，也可传一个或多个文件路径。
.\scripts\ingest.ps1 create 'F:\papers_to_import' --mode codex --run
.\scripts\ingest.ps1 create 'F:\papers_to_import' --mode api --run
# 只处理到待发布状态：
.\scripts\ingest.ps1 create 'F:\papers_to_import' --mode codex --no-auto-publish --run
# 将以下占位值替换为页面或命令输出的真实编号：
.\scripts\ingest.ps1 status <batch_id>
.\scripts\ingest.ps1 run <batch_id>
.\scripts\ingest.ps1 cancel <batch_id>
.\scripts\ingest.ps1 skip <batch_id> <item_id>
.\scripts\ingest.ps1 publish <batch_id>
.\scripts\ingest.ps1 recover
```

原件、检查点和复核结果保存在 `pilot/ingestion/batches/`。停止在当前模型调用完成后生效，已完成步骤可继续复用；程序退出后也可恢复批次。有问题的资料可跳过后继续，原件保留。正式数据先在隔离暂存区构建，通过 SQLite、图谱、向量引用及计数校验后短暂进入维护切换；保留备份与发布日志，失败时恢复旧库。进程在切换中退出时，可在页面点击恢复或运行 `recover`。切换和等待恢复期间，读端暂时返回维护提示。

新文本使用本地 BGE-M3 编码。SQLite、GraphML、Qdrant 的新版本在暂存区构建。发布后，Web 和 Streamlit 均可读取新资料；Web 入库页管理通过模型 API 处理的批次。

2026-11-03 的初次联调：两种模式分别使用同一份公开的 6 页论文，在各自隔离库完成真实模型抽取及独立复核，各发布 1 篇论文、2 条知识和 9 条原文证据，并由真实离线 BGE-M3 生成 18 个向量。SQLite、GraphML、Qdrant 与关键词/语义/图谱证据查询均通过；当时正式库为 26 篇、128 条知识。该历史联调与后续扩充的结果分别记录，报告见 `pilot/reports/ingestion_codex_live_validation.json` 与 `ingestion_paratera_live_validation.json`。

初次入库功能回归为 196 通过、1 项可选测试跳过；前端还验证了密钥来源切换、处理进度、继续/跳过/发布、维护期间冷启动恢复，以及已有问答排序和工艺筛选。真实 API 联调曾因模型自行修复 PDF 断词而被引文校验拒绝；现在重试会收到安全的字段级原因，仍不放宽原文匹配。默认每篇 8 条抽取上限、长批次的时间与调用费用仍应按实际使用评估。

## 本轮混合资料扩充

本轮完成“首批 10 份混合资料验证 → 分批扩充”的流程，共新增 74 份、1,273 页、438 条知识。计划中一份讲稿被发现与另一份大量复用同套实验示例，保留为补充版本但不计数，并以独立的 [SUSS 平台技术规格](https://www.suss.com/en/content/download/2859/35887?version=1)替换；实际批次为 10、16、16、16、15 和补充 1 份。来源选择的初版、修订版和重合页对照均保留。筛选范围是混合键合相关行业，无手机产业限制；目录、摘要、未取得全文和弱相关候选不计入 100 份。

新增资料按实际形式分类：期刊论文 13、会议论文 13、技术演示 23、白皮书 6、技术报告 5、产品技术规格 5、技术手册/指南 4、标准 2、测试方法 2、应用说明 1。新增 60 份为核心研究、14 份为专项支撑；相关性层级不等于可信度评级。每份均有工艺主分类与多维标签。

最终只读审计确认原 26 份目录、分类、128 条知识及原 PDF 哈希保留完整；新增 74 份有 108 个独立复核分块记录，引用来源链以及 SQLite、GraphML、Qdrant 一致性通过。6,537 个本地真实 BGE-M3 向量逐条核对存储矩阵；三条关键词与三条中文语义检索均返回相关新正文。实际检索排名和局限见 `audit_100_relevance.json`，不能把标题页命中或索引一致性当成问答正确率。

抽取由本次实际运行的 Codex 完成：初始 CLI 试跑与后续活跃 `gpt-6.1-sol / ultra` 子 agent 的签名文件交接分开留痕，抽取与复核由不同 agent 执行；没有将脚本或规则生成的结果冒充模型抽取。`scripts/agent_intake.py --watch` 只推进收到的真实回复，本身不生成知识，也不是无人值守模型服务。日常 CLI 与 Paratera API 两种入口继续可用；本轮资料入库未调用 Paratera，真实问答复测则使用既有问答连接。

新增资料形式包括技术手册、白皮书、应用说明、产品技术规格、测试方法和技术演示。知识记录区分实验结果、仿真、作者解释、引用前人结果、厂商规格、工艺建议、标准要求与测试方法。标准保留版本及适用对象，未核实的现行性不会被默认为有效；成品器件鉴定条款不会改写成裸混合键合界面验收规范。

对于少字的标题、品牌、致谢及章节分隔页，只在 agent 实际查看原页并登记 PDF 哈希后保留原文字层；技术图页仍执行严格解析或 OCR。OCR 引用需要复核 agent 实际查看对应原页。跨分块发现分类依据需要补充时，可加入最多 5 个哈希绑定的原页上下文；未发布回复的修订会使旧抽取及复核缓存失效，重新走校验。发布前再次检查所有回复是否仍为已复核版本。

首批真实问答曾暴露标题页/结尾页优先于关键正文的问题，已增加动态实体识别、已复核记录召回及精确引文窗口。AEC-Q104 适用范围、Bruker 吞吐量测量条件和 NPL Ra 局限三题复测已通过，记录见 `qa_first10_retest.json`；这只代表这些具体问题的检查结果，不等于整个资料库的问答准确率。

全库六题真实问答又发现完整英文标题被当作普通词，以及试样字段未参与召回的问题；修复完整标题定位与试样/材料字段召回后，Cu/BCB 的 8×8 mm 切割芯片信息能够命中原文第 8 页。随后收紧附加说明和来源编号格式，消除了扫描长度的措辞冲突及纯数字引用。最终六题的核心事实与实际引用追溯均通过独立 agent 原页核查，报告为 `qa_100_retest2.json` 与 `qa_100_retest2_assessment.json`。初次失败和中间复测没有覆盖；这不是全库准确率或专家金标准。

最终代码全量回归 235 通过、1 项可选测试跳过；最后提示词调整的相关 74 项回归通过。界面实际验证了标准筛选 2/100、产品规格筛选 5/100、产品规格与 D2W 组合筛选 3/100，再恢复全部 100 份与默认折叠标签。报告及真实截图见 `ui_100_validation.json`、`ui_100_top.jpg`。上述扩充报告均位于 `pilot/reports/expansion_20261103/`。

## 运行与重建

已有新增批次后，旧试点 `assemble`、`parse`、`validation.merge` 和硬编码 OCR 命令会明确拒绝执行，避免覆盖新批次。以下旧命令仅用于原始试点复现；新增资料使用上述批量入口。分类侧车重建已兼容 `catalog/classification_batches/`。

已安装项目虚拟环境 `.venv`，实际环境为 Python 3.12.9、CPU 版 PyTorch。直接依赖见 `pyproject.toml`，完整安装快照见 `requirements.lock.txt`，模型版本见 `pilot/model_config.json`。如需重新创建环境，可用本机 `D:\python312\python.exe -m venv .venv`，随后安装锁定依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

以下命令从已有 PDF、解析输出和保存的 agent 标注重建；它们不会自动重新完成文献检索或模型语义抽取。运行时先停止正在进行的界面语义查询，避免本地 Qdrant 存储锁冲突。

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
$env:HF_HOME = Join-Path $PWD 'pilot\model_cache'
$env:HF_HUB_DISABLE_XET = '1'
$env:TEMP = Join-Path $PWD '.cache\tmp'
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = Join-Path $PWD '.cache\uv'
.\.venv\Scripts\python.exe -m kg_pilot.validation
.\.venv\Scripts\python.exe -m kg_pilot.pipeline index
.\.venv\Scripts\python.exe -m kg_pilot.retrieval build
.\.venv\Scripts\python.exe -m kg_pilot.units
```

只有原始 PDF 或解析方法改变时，原始试点才需要重新 `pipeline parse`。若原 PDF 的 SHA-256 不变，可以运行 `ocr_evidence` 复用原试点保存的乱码正文 OCR 输出；若 PDF 已改变，必须先重新 OCR 并核验相关知识和引文，不能将旧 OCR 绑定到新文件。随后运行 `embedding_trial --all` 重新编码和上述建库命令。这些旧 OCR 命令只针对原始试点的已标记资料；新增批次使用前述通用逐页解析、OCR 与显式复核。重新编码会覆盖旧向量，比较实验前应保留旧运行结果。

```powershell
# 常规校验；语义集成测试额外使用已有本地模型，不调用外部模型 API。
$env:KG_PILOT_RUN_SEMANTIC_TEST = '1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\uv.exe pip check --python .venv\Scripts\python.exe

# 对冻结的 16 题重跑检索评估；原题与本轮结果已保存。
.\.venv\Scripts\python.exe -m kg_pilot.retrieval evaluate en
.\.venv\Scripts\python.exe -m kg_pilot.retrieval evaluate zh
```

表格评分重放：`.\.venv\Scripts\python.exe -m kg_pilot.table_benchmark`。该命令读本轮保存的 Docling/MinerU 输出并重跑原生表格抽取，不代表重新运行全部解析器。

`framework_trial` 使用本会话 agent 文件桥接模型回调，必须由活跃 agent 读取请求并写回响应，单独启动脚本不会变成无人值守模型服务。实际请求、响应和检索上下文保存在 `pilot/benchmarks/framework/session_bridge_01/`；不要将它报告为独立 API 性能测试。

## 数据原则

- 原始 PDF、来源 URL、下载状态和文件哈希分别保存。
- 记录文献、样品、工序、测试、结果和证据，不将不同工序或样品的数值合并。
- 未报告的字段保留为空；歧义与解析失败显式标记。
- agent 复核属于自动复核，不称人工或专家确认。
- 未完成的模型调用、性能测试和问答评测不计入通过项。

## 目录

- `pilot/collection/`：分任务采集的全文与来源清单。
- `pilot/catalog/`：去重后的目录、排除记录和获取统计。
- `pilot/parsed/`：带页码和页面位置的证据元素。
- `pilot/annotations/`：agent 抽取、复核记录和测试材料。
- `pilot/indexes/`：可重建数据库、图和检索索引。
- `pilot/reports/`：实际运行结果与技术选型依据。
- `src/kg_pilot/`：处理与评测程序。
- `tests/`：来源定位、条件关联、单位转换等检查。

可解析正文参与检索，知识记录按每份资料的条数上限重点抽取，不代表穷尽所有事实。原始试点的 12 道正例和 4 道证据不足题仅用于小规模诊断，不构成专家金标准或正式准确率。原始参考 PPT 保持不变。
