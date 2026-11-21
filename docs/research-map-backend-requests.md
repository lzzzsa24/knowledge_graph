# 研究地图：给后端（Codex）的接口与数据需求

前端（`web/static`）由 Claude 负责，后端（`web/server.py`、`src/kg_pilot/*`、数据流程）由 Codex 负责。本文件列出研究地图需要后端配合的改动。前端对每一项都做了降级处理：字段缺失时照常显示，字段上线后自动启用。

> 本版替换 2026-11-07 的"流向总览"版需求。文中数字取自 2026-11-07 的接口数据：213 篇文献、1439 条知识记录。

## 现状：研究地图总览怎么用数据

研究地图的默认页（`#/graph?view=overview`，`web/static/research-kg.js`）是一张力导向知识图谱：

- **节点**：文献是方块，知识记录是彩色点（按工艺阶段着色）；另有 7 个**工艺杠杆**圆环（矩阵的行）和 8 个**结果指标**圆环（矩阵的列），位置固定。
- **布局**：每条记录被拉向它所在矩阵格子对应的杠杆和指标，研究同一问题的文献因此靠在一起。
- **淡化**：没有连入任何格子的记录和文献默认淡化。
- **文献标签**：显示"第一作者 + 年份"（如 `Hou 2022`）。
- **其他视图**："更多视图"下另有工艺流向图（`view=flow`，`research-overview.js`）、论文网络和研究脉络。

当前用到的接口和字段：

| 接口 | 使用的字段 | 大小 |
|---|---|---|
| `/api/research-map` | `rows`、`columns`、`cells[].records`（记录→格子）、`cells[]` 计数与 `study_ids`、`directions`、`studies`、`totals` | ≈ 1.5 MB |
| `/api/research-network` | `links[]`（`kind`、`source`、`target`、`label`、`study_id`） | ≈ 0.5 MB |
| `/api/claims`、`/api/papers` | 记录的 `stage / statement / doc_id`；文献的 `title / year / authors` | 全站共用 |

## 需求（按优先级）

### P1 · 扩大"知识记录 → 工艺 × 结果格子"的映射覆盖
这一项决定研究地图是否真正有用。

- **问题**：1439 条记录中只有 **520 条（36%）** 连入了矩阵格子，剩下 919 条只能跟着自己的文献，在图中心挤成一团，默认被淡化。213 篇文献中有 **67 篇**一条记录都没有连入。
- **未连入的 919 条怎么分布**：
  - 按工艺阶段：测量 331、接触键合 186、材料表征 123、表面准备 123、背景 72、键合后退火 50、可靠性试验 34。
  - 按记录类型：**作者报告的实验 353**、测试方法 104、作者解释 102、厂商规格 101、推荐工艺 82、引述前人工作 71。
- **需求**：
  1. **补充映射**：用规则或 agent 补全"记录 → (行, 列)"，重点处理 353 条实验记录。映射口径与现有矩阵一致：只有记录本身声明了工艺或样品条件和对应结果时才连入。
  2. **说明未连入的原因**：仍未连入的记录给出原因代码，例如 `no_process_factor`（未声明工艺条件）、`no_outcome`（没有结果指标）、`background`（背景或综述）、`spec_only`（仅为规格或推荐值）、`ambiguous`（有歧义）。字段建议放在 `/api/research-map` 的 `unmapped: { claim_id: reason }` 中。
- **验收**：
  - 重跑后报告覆盖率（已映射记录数 / 1439、已映射文献数 / 213），并给出新增映射的抽样复核结果。
  - 每条未连入的记录都有原因代码。
  - 原有 520 条映射不变，或者对变化给出说明。

### P1 · 研究地图专用的轻量接口
- **问题**：总览只需要"每条记录属于哪些格子"（约 23 KB）、格子计数和少量说明，却要下载约 1.5 MB 的完整矩阵（含 `record_details`、`source_readings`、`reviewed_records`）和约 0.5 MB 的网络数据。第一次打开慢主要卡在这里。
- **需求**：新增 `GET /api/research-graph`，返回：
  ```json
  {
    "rows": [{ "id": "...", "label": "...", "description": "..." }],
    "columns": [{ "id": "...", "label": "...", "description": "..." }],
    "claim_cells": { "<claim_id>": [["<row_id>", "<column_id>"]] },
    "cells": [{ "row": "...", "column": "...", "documents": 0, "claims": 0, "experiments": 0,
                "simulations": 0, "contrasts": 0, "study_ids": [] }],
    "links": [{ "id": "...", "source": "<doc_id>", "target": "<doc_id>", "kind": "...",
                "label": "...", "study_id": "...", "basis": "..." }],
    "directions": [{ "id": "...", "title": "...", "row": "...", "column": "...", "status": "...",
                     "system_label": "...", "question": "...", "gap": "..." }],
    "studies": [{ "id": "...", "title": "...", "conclusion": "..." }],
    "totals": { "documents": 0, "claims": 0, "mapped_documents": 0, "mapped_claims": 0 },
    "snapshot_id": "..."
  }
  ```
  `links` 只需带上面这些字段，不带 `evidence`。前端点开连线时，再按 `id` 去原接口取依据。
- **验收**：
  - 返回 ≤ 150 KB。
  - `claim_cells`、计数与 `/api/research-map` 完全一致。
  - 数据变化时 `snapshot_id` 随之变化（前端用它判断缓存的布局是否失效）。

### P1 · 规范并补全文献作者
- **问题**：
  - 213 篇中有 **39 篇没有作者信息**，前端只能用标题开头作标签，比如 `Die to Wafer… 2021`。
  - 有 7 篇的 `authors` 是一整段字符串（如 `"Ong JJ, Chiu WL, …"`），其余是列表。
  - 姓名格式混杂，有 `Lin Hou`、`YAN, Meng`、`Ong JJ`、`DE AMICIS` 等几种写法。
- **需求**：
  1. **统一格式**：`/api/papers` 中 `authors` 一律为列表，每项是一个人名。
  2. **补全缺失**：对 39 篇无作者的文献，从 DOI 元数据或 PDF 首页补全；确实无法确定作者的（标准、厂商资料等），在 `organization` 字段给出发布机构，例如 `IPC`、`EV Group`。
  3. **提供短标签**：新增 `short_label`，规则为"第一作者姓 + 年份"（如 `Hou 2022`）；没有作者时用"机构 + 年份"（如 `IPC 2020`）。
- **验收**：所有文献的 `authors` 都是列表；`short_label` 全部非空；无作者的文献数降为 0，或在 `organization` 中注明发布机构。

### P2 · 连线标注依据类型
- **问题**：前端计划用实线表示"原文直接依据"，用虚线表示"规则匹配或 agent 判断"。目前只能根据 `kind` 推测：`citation` 是参考文献匹配，`shared_process` 是规则配对，`study_comparison` 是 agent 审阅。
- **需求**：每条连线增加 `basis`（取值 `source_text` / `rule` / `agent_review`）和 `basis_note`（一句话说明这条关系怎么得到的）。轻量接口和 `/api/research-network` 都要带上。
- **验收**：全部连线都有 `basis`；`repeated_data`、`reused_figure`、`documented_extension` 三类为 `source_text`。

### P2 · 按技术路线筛选
- **问题**：总览准备增加 W2W / D2W / D2D 切换（工艺流向图已经有这个功能）。
- **需求**：`/api/research-graph?route=W2W` 只保留属于该路线的记录、文献和连线，计数按路线统计。
- **验收**：`route=W2W` 时，返回的格子计数与 `/api/research-map?route=W2W` 一致。

### P3 · 研究联系落到具体格子
- **需求**：`study_connections[]` 增加 `cells: [{ "row": "...", "column": "..." }]`，便于在图中把它们定位到具体的"工艺 × 结果"组合；暂时无法确定的给空数组。

## 不再需要（相对上一版）
- `cells[].document_ids`：前端已改为通过记录推出所属文献，不再需要。
- `GET /api/research-map?fields=overview`：由上面的 `/api/research-graph` 取代。

## 约束
- 不改变现有字段的含义和格式；新增字段要向后兼容。`tests/test_research_map_ui.cjs`、`tests/test_research_network_ui.cjs` 依赖现有结构。
- 计数口径保持"有记录连入该组合的文献数"；空白只表示本库未收录，不代表全球没有研究。
- 新增的映射和关系都要能回到原文页（原文引文 + PDF 实际页码），自动结果标注为 agent 自动复核，不称专家确认。
