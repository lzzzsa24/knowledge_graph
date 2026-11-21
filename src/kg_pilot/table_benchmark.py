"""Reproduce the bounded table audit from saved parser outputs and PDF pages.

Run: .venv/Scripts/python.exe -m kg_pilot.table_benchmark
This does not invoke model parsers, install packages, or alter parse/evidence code.
"""
from __future__ import annotations

import importlib.metadata
import json
import re
import unicodedata
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / 'pilot/benchmarks'
REPORTS = ROOT / 'pilot/reports'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class TableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            assert not dict(attrs).get('rowspan') and not dict(attrs).get('colspan'), 'Audit span handling explicitly'
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th'):
            self.row.append(''.join(self.cell))
            self.cell = None
        elif tag == 'tr':
            self.rows.append(self.row)
            self.row = None


def shape(grid):
    return [len(grid), max(map(len, grid), default=0)]


def numeric_equal(a, b):
    """Numbers must occupy their own correctly positioned cell; no decimal repair."""
    scalar = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
    grammar = rf'\s*({scalar})(?:\s*±\s*({scalar}))?\s*'
    aa = re.fullmatch(grammar, unicodedata.normalize('NFKC', a or '').replace('−', '-'))
    bb = re.fullmatch(grammar, unicodedata.normalize('NFKC', b or '').replace('−', '-'))
    if not aa or not bb:
        return False
    return tuple(Decimal(x) if x is not None else None for x in aa.groups()) == tuple(Decimal(x) if x is not None else None for x in bb.groups())


def compare_numeric(expected, grid):
    same_shape = shape(grid) == expected['expected_shape_including_header']
    total = expected['numeric_cells']['count']
    if not same_shape:
        return {'total': total, 'exact_at_expected_coordinates': None, 'mismatches': [],
                'reason': 'Shape differs. Numeric binding score withheld rather than guessing a repair/alignment.'}
    errors = []
    for r in expected['numeric_cells']['rows']:
        for c in expected['numeric_cells']['columns']:
            actual = grid[r][c]
            target = expected['grid'][r][c]
            if not numeric_equal(actual, target):
                errors.append({'row': r, 'column': c, 'expected': target, 'actual': actual})
    return {'total': total, 'exact_at_expected_coordinates': total-len(errors), 'mismatches': errors}


DOCLING_DIR = {
    'ma15051888_p7_t1': 'journals_ma15051888_p7-7',
    'mi13040537_p7_t1': 'journals_mi13040537_p7-8',
    'mi13040537_p8_t2': 'journals_mi13040537_p7-8',
    'ma18225152_p5_t1': 'journals_ma18225152_p5-5',
}

# Findings from original-page visual checks, not inferred merely from string scores.
OBSERVATIONS = {
    'pymupdf_native_tables': {
        'ma15051888_p7_t1': ['Expected 4×8 grid recovered, but decimal points are displaced: 0.12 becomes "012\\n." and 32.5 becomes "325\\n.".', 'The resistance scale/unit row is reordered into fragments such as "(10−. .cm. 2). 8 Ω"; cannot safely bind 10^-8 Ω·cm² automatically.'],
        'mi13040537_p7_t1': ['Expected 6×3 grid recovered. Every decimal value has its dot moved to a separate line, e.g. 1.69 → "169\\n." and 4.9 → "49\\n.".', 'J/m² and Å remain identifiable in the headers.'],
        'mi13040537_p8_t2': ['Expected 5×3 grid recovered. Four integer powers are correct; all eight decimal outcomes have detached decimal dots.', 'W, Å and J/m² remain identifiable in the headers.'],
        'ma18225152_p5_t1': ['Expected 4×4 grid recovered, but mean and uncertainty decimal points are detached, e.g. "13102±341\\n. .".', 'GPa, MPa and dimensionless exponent headings remain identifiable; numerical values are not usable without repair.'],
    },
    'docling_native': {
        'ma15051888_p7_t1': ['3×9 instead of 4×8. Reference headers duplicate/merge; temperature and area rows merge; individual numeric cells contain multiple unrelated values.', 'The resistance unit Ω is rendered as W in "10-8 W.cm 2".'],
        'mi13040537_p7_t1': ['6×4 instead of 6×3. A peer-review page header becomes an extra column. N2 thickness 4.9 is merged into the O2 cell and the N2 cell is empty.', 'An additional partial plasma-power table is spuriously assigned to physical page 7; the source table is on page 8. J/m² and Å are still identifiable.'],
        'mi13040537_p8_t2': ['4×5 instead of 5×3. 75/100 W rows and corresponding numbers are merged; a footer becomes a data cell; a header is duplicated.', 'W, Å and J/m² survive as symbols but cannot repair the row associations.'],
        'ma18225152_p5_t1': ['4×4 table, all nine mean±uncertainty cells, all three state labels and unit associations match the source.', 'Whitespace and degree glyph presentation differ only typographically.'],
    },
    'docling_forced_ocr': {
        'ma15051888_p7_t1': ['Still 3×9. OCR introduces further reference-header errors and merges temperature/area rows.', 'Ω is recognized, but the scale expression is corrupted ("0(i2−8 Ω.cm²)"); forcing OCR does not recover a usable table.'],
        'mi13040537_p7_t1': ['Still 6×4, with page-header contamination and missing N2 thickness association; 4.9 remains merged into O2.', 'Still produces a spurious partial power table on physical page 7.'],
        'mi13040537_p8_t2': ['Still 4×5. 100/150 W merge; 1.82/1.84 merge; 44.4 is corrupted to the character 五.', 'Units remain recognizable but the data-row binding is unusable.'],
        'ma18225152_p5_t1': ['4×4 table and all numeric cells/uncertainties correct; state labels and units semantically intact.', '℃/°C differences are accepted as equivalent temperature glyphs for this benchmark.'],
    },
    'mineru_pipeline_txt': {
        'ma15051888_p7_t1': ['4×8 structure, reference columns and all 21 numeric cells correctly associated.', 'Ω, 10^-8 scale, °C and µm² retained semantically. The HTML flattens some superscripts (e.g. cm2 and 10−8); preserve the audited scale binding separately.', 'Body/caption warning: SiO2 loses its subscript 2 in one prose occurrence and the Figure 7 caption contains duplicated fragments. Table pass is not whole-page text approval.'],
        'mi13040537_p7_t1': ['6×3 structure, gas labels, all ten numeric cells and J/m²/Å correct.', 'Body warning: unrelated fragments from the following page are interleaved as superscripts in paragraphs. Do not replace native evidence text with this Markdown wholesale.'],
        'mi13040537_p8_t2': ['5×3 structure, all twelve numeric cells and W/Å/J/m² correct, including the 150 W final row.', 'Body warning: prose around the table/figure contains duplicated or stray fragments despite the correct table.'],
        'ma18225152_p5_t1': ['4×4 structure, three anneal-state labels, all nine mean±uncertainty cells and units correct.', 'Body warning: SiO2 is rendered as SiO in the prose. The table itself preserves all tested values.'],
    },
}


def main():
    expected_doc = read_json(BENCH/'table_expected.json')
    results, native_raw = [], []
    for expected in expected_doc['tables']:
        tid = expected['table_id']
        doc = pymupdf.open(ROOT/expected['pdf_path'])
        tables = doc[expected['pdf_page']-1].find_tables().tables
        assert len(tables) == 1, (tid, len(tables))
        native_grid = tables[0].extract()
        native_raw.append({'table_id': tid, 'pdf_page': expected['pdf_page'], 'grid': native_grid})
        configurations = [('pymupdf_native_tables', native_grid, 1, str(ROOT/expected['pdf_path']), None)]

        for variant, suffix in [('docling_native', ''), ('docling_forced_ocr', '_forced_ocr')]:
            path = BENCH/'docling'/(DOCLING_DIR[tid]+suffix)/'document.json'
            parsed = read_json(path)
            candidates = [t for t in parsed['tables'] if any(p['page_no'] == expected['pdf_page'] for p in t['prov'])]
            assert candidates, tid
            target = candidates[0]  # first table on each selected physical page is the target
            grid = [[c['text'] for c in row] for row in target['data']['grid']]
            configurations.append((variant, grid, len(candidates), str(path), target['self_ref']))

        stem = Path(expected['pdf_path']).stem
        first_page = 7 if stem in ['ma15051888', 'mi13040537'] else 5
        path = BENCH/'mineru'/stem/'txt'/(stem+'_content_list.json')
        parsed = read_json(path)
        candidates = [t for t in parsed if t.get('type') == 'table' and t['page_idx']+first_page == expected['pdf_page']]
        assert len(candidates) == 1, tid
        html = TableHTML()
        html.feed(candidates[0]['table_body'])
        configurations.append(('mineru_pipeline_txt', html.rows, 1, str(path), {'relative_page_idx': candidates[0]['page_idx'], 'physical_first_page': first_page}))

        for parser_name, grid, count, path, ref in configurations:
            numeric = compare_numeric(expected, grid)
            dimensions_ok = shape(grid) == expected['expected_shape_including_header']
            manual_labels_units_ok = parser_name == 'mineru_pipeline_txt' or (parser_name.startswith('docling') and tid == 'ma18225152_p5_t1')
            if parser_name == 'pymupdf_native_tables':
                manual_labels_units_ok = tid != 'ma15051888_p7_t1'
            passed = dimensions_ok and numeric['exact_at_expected_coordinates'] == numeric['total'] and manual_labels_units_ok and count == 1
            results.append({'table_id': tid, 'doc_id': expected['doc_id'], 'pdf_page': expected['pdf_page'], 'parser': parser_name,
                'source_output': path, 'source_ref': ref, 'expected_shape': expected['expected_shape_including_header'],
                'actual_shape': shape(grid), 'shape_matches': dimensions_ok, 'detected_tables_on_target_page': count,
                'extra_tables_on_target_page': max(0, count-1), 'observed_grid': grid, 'numeric_cells': numeric,
                'row_column_labels_and_units_manual_pass': manual_labels_units_ok, 'table_pass': passed,
                'observations': OBSERVATIONS[parser_name][tid]})

    summary = {}
    for parser in OBSERVATIONS:
        own = [r for r in results if r['parser'] == parser]
        summary[parser] = {'tables_tested':len(own), 'tables_passed':sum(r['table_pass'] for r in own),
            'exact_numeric_cells_on_shape_valid_tables':sum(r['numeric_cells']['exact_at_expected_coordinates'] or 0 for r in own),
            'numeric_cells_scored':sum(r['numeric_cells']['total'] for r in own if r['numeric_cells']['exact_at_expected_coordinates'] is not None),
            'numeric_cells_not_scored_due_to_invalid_shape':sum(r['numeric_cells']['total'] for r in own if r['numeric_cells']['exact_at_expected_coordinates'] is None)}
    package_versions = {p:importlib.metadata.version(p) for p in ['pymupdf','docling','rapidocr','mineru','torch','pypdfium2']}
    assessment = {
        'schema_version': 1, 'date': '2026-11-02', 'status': 'completed_bounded_diagnostic',
        'scope': {'documents':3, 'physical_pages':4, 'tables':4, 'target_numeric_cells':52,
            'sampling': 'Purposive diagnostic pages: known failures plus one well-formed control; all three source papers are from MDPI.',
            'ground_truth': 'pilot/benchmarks/table_expected.json', 'manual_truth_is_not_independent_human_gold':True},
        'method': {'pass_rule':'Correct table dimensions, all designated numeric cells including ± terms, row/column-label and unit binding, and no spurious extra table on the page.',
            'numeric_comparison':'Decimal values compared at expected row/column coordinates; whitespace around whole scalar values allowed; no repositioning of detached decimal points. Unicode-equivalent glyphs accepted for benchmarking only, not for exact quotation validation.',
            'invalid_shape_policy':'Do not invent row/column repair: numeric coordinate score is null when dimensions differ.',
            'out_of_scope':['full-document factual extraction accuracy','all tables in the 26-paper corpus','cross-publisher generalization','formula recognition','scan-only tables','fair throughput ranking']},
        'versions':package_versions,
        'runs':{
            'docling_native':read_json(REPORTS/'docling_benchmark.json'),
            'docling_forced_ocr':read_json(REPORTS/'docling_ocr_tables.json'),
            'mineru':[
                {'doc_id':'journals_ma15051888','physical_pages':[7],'status':'previous completed run, output reused','configuration':'pipeline / txt / formula false / table true'},
                {'doc_id':'journals_mi13040537','physical_pages':[7,8],'zero_based_start_end':[6,7],'status':'completed, exit code 0','local_server_started':'2026-11-02 18:52:07.640 +08:00','batch_completed':'2026-11-02 18:53:38.086 +08:00','server_start_to_batch_complete_seconds':90.446},
                {'doc_id':'journals_ma18225152','physical_pages':[5],'zero_based_start_end':[4,4],'status':'completed, exit code 0','local_server_started':'2026-11-02 18:52:07.646 +08:00','batch_completed':'2026-11-02 18:53:37.052 +08:00','server_start_to_batch_complete_seconds':89.406}
            ],
            'mineru_command_template':'.venv/Scripts/mineru.exe -p PDF -o pilot/benchmarks/mineru -b pipeline -m txt -s START_0 -e END_0 -f false -t true',
            'environment':{'HF_HOME':'pilot/model_cache','MODELSCOPE_CACHE':'pilot/model_cache/modelscope','TEMP':'.cache/tmp','TMP':'.cache/tmp','new_dependencies_installed':False},
            'execution_notes':['Initial sandboxed MinerU attempts failed creating a subdirectory below project TEMP with Windows WinError 5. Authorized escalated reruns succeeded.', 'Two new MinerU jobs ran concurrently with cached models and local API startup. Durations are execution records, not apples-to-apples parser speed measurements.']},
        'summary':summary, 'table_results':results,
        'render_check':{'backends':['PyMuPDF','pypdfium2'], 'directory':'pilot/benchmarks/render_comparison',
            'finding':'Both direct renderers show the four source tables cleanly. The duplicated/merged Docling output is not present in those rendered source pages. Exact origin within parser/backend processing is not established.'},
        'recommended_routing':[
            {'when':'Native PDF text passes quality checks and source-page sampling','route':'PyMuPDF text blocks with physical page/bbox as evidence backbone','restriction':'Do not equate a readable text layer with correct native table extraction; this test found detached decimals.'},
            {'when':'A table contains quantitative facts required by claims','route':'MinerU pipeline txt targeted to the physical page, then validate row labels, numeric cells, unit multiplier, and footnotes against the original page','restriction':'Import the accepted table only. Do not replace the full native text with MinerU Markdown; prose contamination and lost subscripts were observed.'},
            {'when':'Native text is garbled or missing','route':'Docling + RapidOCR as an OCR candidate, explicitly tagged with parser provenance; verify quotations on rendered source pages','restriction':'The small_die example supports this fallback, not a blanket OCR guarantee. Forced OCR did not fix the three problematic table pages.'},
            {'when':'Parsers disagree, dimensions/units fail, or important denominator/uncertainty is ambiguous','route':'Quarantine the extraction; use agent visual transcription with source evidence and independent review','restriction':'Do not silently correct Ω from W, restore decimal positions, or discard row/column conditions.'}
        ],
        'limitations':['Only 4 tables from 3 MDPI papers, deliberately selected; no general ranking or corpus-wide accuracy estimate.', 'Ground truth was transcribed and visually checked by an agent, not independently double-annotated human data.', 'MinerU formula recognition was disabled; equations and semantic material formulas were not benchmarked.', 'Source internal inconsistency: ma18225152 Table 1 says 131.02±3.41 GPa while prose says 131.2 GPa; table ground truth is retained without repairing the paper.', 'Docling model/backend parameter tuning and alternative native backends were not exhausted.', 'Version-, PDF- and configuration-specific results; expand a held-out multi-publisher set before final stack selection.']
    }
    write_json(BENCH/'table_benchmark_native.json',native_raw)
    write_json(REPORTS/'parser_assessment.json',assessment)

    labels = {'pymupdf_native_tables':'PyMuPDF find_tables','docling_native':'Docling 原生模式','docling_forced_ocr':'Docling 强制 OCR','mineru_pipeline_txt':'MinerU pipeline/txt'}
    lines = [
        '# 小规模解析器对照（2026-11-02）', '',
        '本轮建议保留 PyMuPDF 的原生正文作为证据骨架，定量表格优先尝试 MinerU 的逐页解析并对照原页核验；乱码正文再使用显式标注来源的 Docling/RapidOCR。暂不将任何一种解析器设为整篇论文的唯一可信输出。', '',
        '这项结论只基于 3 篇 MDPI 论文的 4 页、4 张表。页面是为暴露已有错误而选取，并含一张对照表，不能据此估计 26 篇语料的总准确率，也不能推广为产品或所有出版商的排名。', '',
        '## 实测结果', '',
        '| 配置 | 表格完整通过 | 结构有效时数值单元格正确/已评分 | 结构失效而未评分 |',
        '|---|---:|---:|---:|'
    ]
    for parser,s in summary.items():
        lines.append(f"| {labels[parser]} | {s['tables_passed']}/{s['tables_tested']} | {s['exact_numeric_cells_on_shape_valid_tables']}/{s['numeric_cells_scored']} | {s['numeric_cells_not_scored_due_to_invalid_shape']} |")
    lines += ['', '共设 52 个数值单元格；其中 9 格各含均值及 ± 项，必须一起正确。完整通过还要求行列标签、单位和倍率正确，且没有额外伪表。尺寸不符时不猜测对齐、不评分数值，不能把“9/9”误读成 Docling 在全部表格上无误。', '',
        '| 原始物理页 | 期望结构（含表头） | 主要观察 |', '|---|---|---|',
        '| ma15051888，p7，表 1 | 4×8 | PyMuPDF 小数点脱离；Docling 变成 3×9，Ω→W，强制 OCR 仍错；MinerU 21 格数值及文献列关联正确。 |',
        '| mi13040537，p7，表 1 | 6×3 | Docling 多出页眉列，4.9 串入 O2 行，并虚构一张本应在下一页的部分表；MinerU 10 格及 Å、J/m² 正确。 |',
        '| mi13040537，p8，表 2 | 5×3 | Docling 变成 4×5，75/100 或 100/150 行合并；强制 OCR 将 44.4 识别为“五”；MinerU 12 格正确。 |',
        '| ma18225152，p5，表 1 | 4×4 | Docling 两种配置和 MinerU 均保留 9 个均值±误差单元格；PyMuPDF 小数点与数值分离。 |', '',
        'PyMuPDF 的表格尺寸在 4 页上都正确，却仅有 17/52 个指定数值格可直接解释。这个例子说明尺寸检查无法替代小数点、单位和行列绑定检查。MinerU 的表格通过也不代表正文通过：mi13040537 的正文混入重复及异常上标片段；ma18225152 的正文把 SiO2 写成 SiO。MinerU 表头中的部分上标变为普通字符，10^-8 Ω·cm² 的倍率应从已核验表头单独保存。', '',
        '## 证据与执行方式', '',
        '- [原页核验基准](../benchmarks/table_expected.json)：由 agent 判读并记录完整行列、全部主要数值、单位倍率、原始 PDF 页码及范围说明。',
        '- [机器可读报告](parser_assessment.json)：每种配置的实际网格、逐格差异、原输出位置和版本。',
        '- [原生表格重跑结果](../benchmarks/table_benchmark_native.json)；[原页双渲染对照](../benchmarks/render_comparison/)。',
        '- MinerU 新补跑为 mi13040537 p7–8（CLI 0 基 6–7）及 ma18225152 p5（0 基 4），均正常退出；ma15051888 p7 使用本轮已完成的保存结果。',
        '- 新补跑使用已安装模型与依赖，pipeline/txt、开启表格、关闭公式。Windows 受限运行在项目 TEMP 子目录创建时返回 WinError 5；授权的非受限本地重跑成功。没有安装新依赖。',
        '- 两项 MinerU 并发重跑从本地服务启动到批次完成约 90 s，含启动且非独占运行；Docling 保存计时也有不同启动/缓存边界，故不做速度优劣排名。', '',
        '版本：' + '；'.join(f'{k} {v}' for k,v in package_versions.items()) + '。', '',
        '两种直接渲染后端（MuPDF 与 PDFium）均显示原表正常。现有证据只能确认 Docling 输出发生交叠/串行，不能确定其内部具体根因。另有来源自身不一致：ma18225152 表 1 的初始弹性模量为 131.02±3.41 GPa，正文为 131.2 GPa；本基准保留表值，不把论文差异归罪于解析器。', '',
        '## 本试点的路由与后续边界', '',
        '1. 正文先用 PyMuPDF，保存原始字符、物理页和 bbox；规范化副本用于检索，不能覆盖逐字引用。',
        '2. 表格按页调用 MinerU，只导入通过原页核验的表格。检查表名、样品行、条件列、倍率、单位、±、脚注、分母以及“引用文献/本文结果”归属。',
        '3. 原生正文乱码时尝试 Docling/RapidOCR，并保留 OCR 标识。本语料 small_die 的成功恢复是单一案例；对结构错误表格强制 OCR 未形成可靠补救。',
        '4. 不一致结果进入待复核区，由 agent 对照原页转录并独立复核。禁止用猜测自动补回小数点、把 W 改成 Ω、或将多个实验条件合并。',
        '5. 最终选型前扩大到未参与调试、来自不同出版商的表格和扫描页，另测公式、正文、阅读顺序及端到端知识抽取。本轮既未完成这些评测，也未穷尽 Docling 的后端和参数调优。', '',
        '复现评分：在项目环境设置 `PYTHONPATH=src` 后运行 `.venv/Scripts/python.exe -m kg_pilot.table_benchmark`。该脚本读取保存的 Docling/MinerU 输出并重跑 PyMuPDF 表格抽取，不安装依赖、不重新下载模型、不修改主解析代码。'
    ]
    (REPORTS/'parser_assessment.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
