# 研究地图后端交接

对应 `research-map-backend-requests.md`，2026-11-07 实施。本轮由 Codex 修改后端与数据复核；前端由 Claude 接入。验收数据见 `pilot/reports/research_backend_20261107/backend_acceptance.json`。

## 本轮验收结果

- 映射记录从 520 增至 531 / 1,439（36.90%），映射文献从 146 增至 147 / 213（69.01%）。旧 814 个归格关系全部保留，新增 12 个关系，涉及 11 条记录；908 条未归格记录全部给出原因。详见[原页复核报告](../pilot/reports/research_backend_20261107/coverage.md)。
- 213 份资料均有列表形式作者和短标签；其中 188 份有个人作者，25 份以机构著录。详见[作者复核报告](../pilot/reports/research_backend_20261107/paper_metadata.md)。
- 全库轻量 JSON 为 **148,167 字节**，gzip 传输为 **19,698 字节**；均低于 150,000 字节。三条路线下的格子、记录、总计与原矩阵逐项一致，281 条连线均有依据类型，且能按 ID 取回原页证据。
- Python 回归 **851 项通过、1 项跳过**，另有一条上游依赖弃用提示。`tests/test_research_map_ui.cjs` 与 `tests/test_research_network_ui.cjs` 均通过现有浏览器交互检查；网络测试的旧固定 280 条预期改为校验接口实际返回数，仍校验无证据关系被拒绝。

## 总览入口

`GET /api/research-graph`，可选 `?route=W2W`、`D2W`、`D2D`。其他路线值返回 422。

返回需求中的 `rows`、`columns`、`claim_cells`、`cells`、`links`、`directions`、`studies`、`totals`、`snapshot_id`。`claim_cells` 与同路线 `/api/research-map` 的 `cells[].records` 严格一致。计数仍表示本库中有记录连入该格的资料数，格子之间不能相加。

额外字段：

- `document_ids`：当前路线内的全部文献，包含尚未归格者。前端据此筛选全站 `/api/papers` 和按 `doc_id` 筛选 `/api/claims`，避免路线切换后残留其他路线的淡化点。
- `route`：当前路线；全库为 `null`。路线沿用文献级分类，不能据此推断一篇论文的所有子试验都用了该路线。
- `network_snapshot_id`：连线证据版本，用于下面的详情请求。它与布局用的 `snapshot_id` 用途不同。

不返回 `record_details`、原页长引文或网络完整 `evidence`。浏览器会自动协商并解压 gzip；普通客户端不请求 gzip 时也能得到相同完整 JSON。支持 `ETag / If-None-Match` 和 304，按编码设置 `Vary: Accept-Encoding`。缓存版本包含目录、记录、分类、复核覆盖及后端算法；输入变化会更换版本。

## 连线证据与类型

轻量接口与 `/api/research-network` 的每条连线均有 `basis`、`basis_note`：

| kind | basis | 含义 |
| --- | --- | --- |
| `citation` | `source_text` | 原文书目明确引用；不表示两篇的结论互相支持 |
| `repeated_data`、`reused_figure`、`documented_extension` | `source_text` | 原页定位的重复数据、复用图或研究延续，仍需查看关系自身的范围限制 |
| `shared_process` | `rule` | 同格、同材料体系的实测记录按规则配对，不证明相同试样或因果 |
| `study_comparison` | `agent_review` | agent 按原页组织的研究对照，未经专家确认 |

`source_text` 指关系的直接文本依据，并不表示这条关系不曾经由自动抽取或复核。路线筛选保留全库网络中已有的连线 ID，不重新生成一组无法在原接口查到的边。

点击后请求：

```text
GET /api/research-network/links/{link_id}?snapshot_id={network_snapshot_id}
```

返回完整连线，含 `evidence`、`scope`、`basis`、`basis_note` 和当前 `snapshot_id`。版本已更新返回 409，提示刷新总览；连线不存在返回 404。省略版本参数时取当前版本。`/api/research-network` 原有完整响应继续可用。

## 未归格记录

`/api/research-map` 新增 `unmapped: {claim_id: reason}` 与 `unmapped_reason_labels`。在对应路线范围内，每条记录要么出现在格子里，要么有未归格原因；不会同时出现于两处。

| reason | 解释 |
| --- | --- |
| `no_process_factor` | 本条记录未明确当前矩阵行所定义的工艺或样品条件 |
| `no_outcome` | 未识别到当前矩阵列定义的结果；晶粒尺寸、计量误差等可能是有价值的实测，但不属于当前列 |
| `background` | 背景、综述或转述前人工作，尚未归入具体组合 |
| `spec_only` | 规格、推荐工艺或规范要求，尚未建立具体组合 |
| `ambiguous` | 归格依据有歧义、被复核保留或缺少有效证据 |
| `out_of_scope` | 仅在指定材料体系时使用，表示不属于当前体系 |

原因是当前坐标轴和抽取记录下的诊断，不是对整篇论文是否有实验或结果的判定，也不代表全球研究空白。新增关系的 `reviewed_records` 带工艺原文、结果原文、PDF 物理页、适用范围、限制和 `automatic_agent_reviewed` 标记。新增复核单独保存于 `pilot/research_map/coverage_reviews.json`，不覆盖旧的签署决定。

## 文献标签

`GET /api/papers` 的 `authors` 总是字符串列表；有个人署名时每项一个人名。无个人署名的标准、产品资料等保留空列表，并提供真实 `organization`。`short_label` 为首作者姓或机构加年份，含复姓修正。

4 份未能确定出版年的资料使用 `n.d.`。另有 4 份从来源确认的年份只用于短标签，原有 `year` 字段仍保留原值，前端不应把 `short_label` 拆开当年份字段。

显示元数据保存在 `pilot/catalog/paper_metadata_overrides.json`，按文献编号和 PDF SHA-256 绑定。原始目录中的引用身份信息不变，避免作者规范化让既有书目匹配签名失效。新增资料仍需相同的来源复核才能补充未知作者。

## 研究联系定位

`/api/research-map` 和 `/api/research-notes` 的 `study_connections[].cells` 为 `[{row, column}]`。只使用该联系已绑定、内容校验仍有效的 `member.claim_ids` 在矩阵中的格子，取并集并去重；不是两篇所有主题的交叉组合，也不声称两边都测过每一格。仅有复用图片等依据、无法定位记录时返回 `[]`。

## 复查入口

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/check_research_graph.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_research_graph.py --base-url http://127.0.0.1:8600
.\.venv\Scripts\python.exe -X utf8 scripts/audit_paper_metadata.py
```

第一个命令在进程内检查全部路线、归格、计数、作者、连线证据及缓存，不调用生成模型；第二个检查已经启动的实际服务。报告分别记录完整 UTF-8 JSON 字节数和 gzip HTTP 传输字节数，避免混用两种口径。`elapsed_seconds` 可能命中缓存，仅表示当次调试耗时，不是冷启动性能评测。Python 后端改动后需重新启动服务。
