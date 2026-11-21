"""Serialize root agent's source-page and visual review; no corpus writes."""
import json, hashlib
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'pilot/reports/quality_review_20261113'
papers={p['doc_id']:p for p in json.loads((ROOT/'pilot/catalog/papers.json').read_text(encoding='utf-8'))}
raw={c['claim_id']:c for c in map(json.loads,(ROOT/'pilot/annotations/claims.jsonl').read_text(encoding='utf-8').splitlines())}

def ev(page,quote):return {'pdf_page':page,'quote':quote}
def claim(doc,n,conclusion,conditions,denominator,method,limits,comparison,decision='usable_with_conditions',extra=()):
    cid=doc+f'_c{n:03}'
    return {'claim_id':cid,'conclusion':conclusion,'source_evidence':[{'pdf_page':e['pdf_page'],'quote':e['quote']} for e in raw[cid]['evidence']]+list(extra),'conditions':conditions,'denominator':denominator,'method':method,'limitations':limits,'system_comparison':comparison,'decision':decision}
def issue(severity,description,ids,evidence):return {'severity':severity,'description':description,'claim_ids':ids,'evidence':evidence}
def document(i,group,pages,visual,assessment,claims,issues,use):
    p=papers[i]
    return {'doc_id':i,'title':p['title'],'group':group,'source_path':p['local_path'],'source_sha256':hashlib.sha256(Path(p['local_path']).read_bytes()).hexdigest(),'reviewed_pages':pages,'visual_pages':visual,'document_assessment':dict(zip(['source_identity','research_method','extraction_fidelity','applicability'],assessment)),'claims':claims,'issues':issues,'recommended_use':use}

docs=[]
i='doc_37f8496745bd913db74a'
docs.append(document(i,'guidance',[1,5,10,14,20,22,24,31,41],[20,24],[
    '原件封面/修订表均为AEC-Q104 Rev A，2025-11-28；本次官方PDF哈希与本地一致。AEC目录HTTP403/HTTPS握手失败，现行性保留未确认。',
    '规范要求而非实验结果。重点回看适用对象、试验表横向列、批次数、样本数和接受准则。',
    '所核c001/c005/c006/c007文字忠实，c007已提示需D/G/K/M注释；尚未把全表与注释完整并入单条数据。',
    '汽车成品MCM的鉴定规划支持；不能当作裸Cu/介质界面的普适失效阈值或已通过认证。'],[
    claim(i,1,'限定为直接焊接到PCB的MCM；成品组件层级。','汽车用成品多芯片模块','不适用：规范范围','p5范围+p10层级','不能将满足某一个试验当作通过整份AEC鉴定。','原claim及catalog已准确限定。',extra=[ev(10,'This document focuses only on qualification of the completed MCM component.')]),
    claim(i,5,'TC 1000 cycles是试验要求；需带试样/批次及前后检测条件。','环境工作温度范围；SMT先PC；D/G注释；高温电测；封装样品声学检查','表A4：每批按AEC-Q100/Q101/Q200，最低30/批或客户协商；3批，0失效','表1 A4，物理p20，印刷p16','3批0失效是接受准则，绝非寿命或良率实测；仍需完整资格计划。','c005正确抽取1000及预处理，但单条没有表中批次、样本量、0失效及所有后续检查。',extra=[ev(14,'Sample sizes used for qualification testing and/or generic data submission must be consistent with the specified minimum sample sizes and acceptance criteria in Table 1.')]),
    claim(i,6,'声学分层限制针对特定monolithic construction表贴MCM。','J-STD-020范围；注释P；TC后，线焊区域或热性能超规格','表C8：10 MCM/批，3批','表1 C8物理p24','不是所有HB界面不得有任何可检出空洞。','c006已带关键对象限制；补齐3批列和每批10。'),
    claim(i,7,'HTOL 1000 h为资格试验时长。','最高环境工作温度，最大Vcc；D/G/K/M；按室温、低温、高温顺序前后测','表B1：最低30/批或协商，3批，0失效','表1 B1物理p22','不能推出产品寿命≥1000h或全寿命无故障；未展开各注释，工程使用前必须补全。','c007已把缺失注释明示为限制；保留而非误判错误。')],[
    issue('P2','试验值需与整行条件、脚注绑定；单独检索1000 cycles可能丢失样本量/适用性。',[i+'_c005',i+'_c006',i+'_c007'],[ev(14,'End-point test temperatures (e.g., room, hot and/or cold) are specified in the "Additional Requirements" column of Table 1 for each test.')]),
    issue('P2','当前版本确认未闭环：已核官方原件但官方目录本轮访问受阻，不能写“已核实最新”。',[],[])],
    '保留为支持性规范；工程方案引用须展开展示完整试验行和注释，现行性另列待确认。'))

i='doc_09a84fea9df826945693'
docs.append(document(i,'guidance',list(range(1,10)),[1,7],[
    'NIST官方资料页身份匹配；2014校准程序说明，不是混合键合产品标准。',
    '报告触针系统的校准链、带宽及不确定度预算，适合指导报告测量条件。',
    '所核原有6条范围和单位总体忠实；p1原生PUA微米已在发布引文修复，视觉µm。',
    '适用于该触针测量系统与条件，不能把其µm级针尖/横向带宽当AFM或HB验收阈值。'],[
    claim(i,1,'λc=0.8 mm，λs=2.5 µm是测量滤波设置。','采样间距0.125 µm，通常4 mm评价长度；ASME B46.1-2009所述Gaussian滤波','不适用：方法参数','触针轮廓法','Ra/Rq(线)不能直接等同Sa/Sq(面)，不同带宽不可只比较单数值。','c001未把截止值误写成粗糙度阈值；单位视觉核对正确。'),
    claim(i,2,'触针半径1.52±0.15 µm，k=2。','半径由标准线及刀口轨迹校准','不适用：校准参数','p1实物触针与形貌修正','µm是探针几何尺度，不是高度分辨率；小于尖端半径的空间波长灵敏度降低。','c002已正确标注k和用途；与c005联动显示。'),
    claim(i,3,'扩展不确定度U=2uc；uc包含系统与随机变化项。','该文件的校准测量，按表1–3系统类型选公式','不适用；不是研究样本量','p2定义、p7预算表','不能把u(I)或单个噪声项当最终U，也不能把k=2说成无条件精确95%。','c003正确；p7图表公式可读，建议保留公式图/结构化预算。'),
    claim(i,6,'客户使用校准件时需另评使用阶段不确定度。','客户具体仪器、环境、操作者及标准','不适用','p5 Note','NIST校准证书不等于所有后续测量误差已被覆盖。','c006忠实，保留为跨论文计量比较规则。')],[],
    '可用于建立测量条件模板；所核条目未发现需纠正的实质错误。'))

i='doc_e32cb835d14010241a5a'
docs.append(document(i,'guidance',list(range(1,6)),[1,4],[
    '官方IPC/Global Electronics Association下载PDF与本地SHA一致；PDF标5/20，当前官方Approved索引条目标08/20，记录差异，不能凭索引把PDF改成8月。',
    '金属箔非接触面形貌建议方法；不是HB工艺验收规范。',
    'c002与c006/c008分别忠实于正文和附录，但缺少一条共同提示其操作要求不一致的约束。',
    'PCB金属箔计量支持；200,000 µm²、50X及µm级滤波不直接转用为亚纳米HB阈值。'],[
    claim(i,1,'规定金属箔/标准样最小扫描面积200,000 µm²。','50X，长宽比≤5:1，可能需要拼接','代表性扫描面积；不是样本件数','正文5.1','大面积/光学方法背景不能省略。','c001范围和单位正确。'),
    claim(i,2,'正文5.2写No filters。','正文段落5.2','不适用','p1正文','同份文件附录又给滤波设置，具体执行不能自动择一。','原c002注明正文；建议与c006/c008绑定提示内在差异。','requires_conflict_context'),
    claim(i,6,'附录A.2.2 S-filter=2 µm。','50X，NA0.95，200×1000 µm扫描区域','不适用','p4附录','不宜与正文5.2拼成单一无冲突流程。','c006保留附录和配置，正确但应展示冲突。','requires_conflict_context'),
    claim(i,8,'附录A.2.2 L-filter=0.2 mm。','同上；F-operation为平面校正','不适用','p4附录','需请求规范解释或明确具体测量方案，不能由系统默默裁决。','c008数字忠实；用于问答需与c002一同检索。','requires_conflict_context')],[
    issue('P1','同份方法正文“不用滤波器”与附录滤波设置并存；事实抽取正确仍可能生成互相矛盾的实验步骤。',[i+'_c002',i+'_c006',i+'_c008'],[ev(1,'No filters shall be used with this test method.'),ev(4,'S-filter: 2 µm')]),
    issue('P2','索引日期08/20与PDF5/20不同，内容哈希一致；需保留两个来源字段而不是自动改版次。',[],[])],
    '保留方法与冲突说明；暂停把它生成无条件的唯一滤波执行流程。'))

i='doc_0596f8392fed699deb6e'
docs.append(document(i,'guidance',[1,2],[2],[
    '2022-10 SET NEO HB官方2页数据表；当前产品页仍列±0.5 µm@3σ。标称日期不等于2026现行全部配置保证。',
    '厂商规格，无本表独立测量样本、重复数、误差分布或第三方验收报告。',
    'c001–c008已明确manufacturer_specification并保留各规格未必同时成立；关键c002/c003视觉核对正确。',
    '设备筛选和需求沟通，不是工艺良率或实际满载产能实验。'],[
    claim(i,2,'标称键合后精度±0.5 µm@3σ。','配置、应用、操作者相关','样本量/分布未报告；3σ为厂家规格表述','规格表及脚注','不是保证所有键合都在±0.5µm，不是独立验证后的量产能力。','c002已保留限制；该证据类别应在地图和问答一致显示。'),
    claim(i,3,'1000 UPH为dry cycle空循环。','各规格可能不同时成立','不是合格产品数/时；实物满载参数未报告','规格表Average cycle time','不能与最大精度组合成1000个合格混合键合产品/时承诺。','c003准确保留dry cycle和配置限制。'),
    claim(i,4,'键合臂力上限300N。','具体工艺/配置相关','不适用；非单位面积压力','设备规格','没有实际接触面积，不能直接换算成MPa。','c004正确；应避免与论文局部接触压力直接排名。'),
    claim(i,8,'全自动配置洁净等级标ISO3。','独立机配置为ISO5','不适用；非成品缺陷率','厂商规格','洁净等级不等价键合表面污染实测值。','c007/c008已区分配置。')],[],
    '保留厂商规格；所核关键条目未发现实质错误，不把企业属性本身当作不可信。'))

i='doc_fbaae2350a7f8e8f8561'
docs.append(document(i,'comparison',list(range(1,11)),[6,7],[
    '2016 Micromachines 7,234；DOI及PMC来源匹配。德国/日本合作作者组，对本轮nt-Cu系列提供不同体系的实测对照，非同条件独立复现实验。',
    '多预处理方法对照；图4/5各条件9颗芯片，未报告独立晶圆批次数；图有误差棒但正文/图注未定义SD/SE/CI。部分低温样品切割时先脱离，存在可测样品筛选。',
    '已抽取300°C和310MPa及测试几何限制，但全流程最高预处理温度350°C、n=9、失效位置和24h仅TDS未与关键记录绑定。',
    'Cu框形热压键合，非图形化Cu/介质协同HB；不可与其他方法的界面本征拉伸强度直接排名。'],[
    claim(i,2,'H2/Ar预处理后暴露30min，300°C/30kN/60min后的芯片拉伸测值310MPa。','PH-350预处理350°C/30s；不是全流程≤300°C','图5每条件9颗；独立晶圆/批次数未报告','芯片拉伸；Si弯曲/角点应力集中；图6所述甲酸和H2/Ar两组均从Si基底或Ta层断裂','310MPa不是Cu-Cu界面本征强度，不能把30kN当压力；未定义误差棒。','raw c002+c004及现有semantic review已保留几何警告；仍应绑定最高预处理温度与n。',extra=[ev(4,'PH-350'),ev(4,'350 ◦C'),ev(4,'30 s'),ev(6,'Nine chips were tested in each condition.'),ev(7,'In the case of formic acid vapor and H2/Ar plasma pre-treatments, the chips were all fractured from the Si substrate or Ta layer, which indicates that the Cu-Cu interfaces are strongly bonded.')]),
    claim(i,5,'NH3及两种SAM在本文300°C条件下没有明显优于柠檬酸。','处理方案/暴露30min；SAM在200°C尝试脱附，文中50µm对置间隙对应甲酸原位处理/SAM脱附，非所有预处理','每组9芯片，未定义统计显著性检验','图5强度与图6断裂位置','说明保护层策略依赖去除工艺；不能推广为NH3/SAM永远无效。','c005正确；这是值得保留的反例，避免只抽最优数据。'),
    claim(i,7,'24h暴露后H2脱附谱变化不显著，属于TDS观测。','独立Ta20nm/Cu750nm薄膜样片，60°C/min升温','TDS重复数未报告','TDS，不是24h暴露后键合强度测试','24h仍可高强度键合是作者预测，不是本文证实的310MPa工况。','c007准确标TDS；应阻止与c002的30min310MPa合并。'),
    claim(i,8,'吸附氢抑制氧化为作者解释。','基于TDS及断裂表现','不适用','机理推断','不能升格成独立直接证实Cu-H保护层全部化学结构的事实。','claim_type已经author_interpretation，保留。')],[
    issue('P2','全热预算、样本量和删失/失效位置尚未与高强度记录一起呈现，比较时容易误读。',[i+'_c002',i+'_c003'],[ev(5,'It should be noted that the measured tensile strength using this setup is not the actual tensile strength of the Cu-Cu bonded interface.')]),
    issue('P2','24h TDS结果不能补写成24h暴露后310MPa键合强度。',[i+'_c002',i+'_c007'],[ev(9,'It is suggested that high-strength Cu-Cu bonding is possible even when exposed for 24 h.')])],
    '适合对照表面保护方法与提出再验证实验；保留未优于对照和切割脱离数据，结论限本文结构/工况。'))

i='doc_514ae02d22f308b098a2'
docs.append(document(i,'comparison',list(range(1,12)),[3,7],[
    '2015 Scientific Reports 5:9734，NYCU机构存档，DOI匹配；与2022 ma15051888共享Chih Chen/King-Ning Tu，与2024 Nature Communications（doc_f5292e6b6d0e24d12f59，10.1038/s41467-024-51510-7）共享King-Ning Tu，不能称完全独立团队复现。',
    '四种Cu膜、不同取向/粗糙度/沉积/设备/接合面积，存在协变量；电镀3×3mm²夹具与溅射8×8cm²机台不可混用。拉伸仅对溅射片。',
    'c003已准确称82%面积，c004已保留175kg机器上限和未断裂；模型适用限制与5个拉伸试样的完整结果未绑定。',
    '纯Cu直接热压键合，不是Cu/介质图形化混合键合。对“无空洞”与“强度”口径有用，不能提供量产电良率。'],[
    claim(i,2,'200°C30min所示FIB截面几乎无残余空洞。','电镀nt-Cu/nt-Cu，局部截面','观察区域，不是整片或die数','FIB/EBSD','p2同样说150°C局部无空洞；p7图8f另一个平面视野仍有残余空洞，不能宣称提高温度消灭全片空洞。','原claim限截面正确；既有semantic review已将夸大温度因果的对比判uncertain。'),
    claim(i,3,'82%是图7a声学图像的良好键合面积比例。','溅射97%(111)Cu，200°C1h，8×8cm²片','总成像面积，非合格器件数','SAM','不能转成82%电学良率或全晶圆良率；文中SAM分辨描述需再核仪器。','c003口径准确；与电镀nt-Cu小截面结果分开。'),
    claim(i,4,'175kg是机器上限时未断裂的载荷，不是断裂强度。','2×2cm²溅射样片；拉伸前粘胶150°C2h','p6共5试样：3件在上限未断，2件在2.7/1.3MPa断；Fig.7c示例件多数断裂在Si界面，另一断裂件位置未给','受上限截尾的拉伸测试','不能只取3件称5件全过、也不能求无删失的平均强度；原kg不直接写成应力。','c004已有未断限制，建议补3/5及剩余2件和破坏位置。',extra=[ev(6,'The other two specimens fractured at 2.7 MPa and 1.3 MPa.')]),
    claim(i,8,'键合时间反比应力/扩散率仅属所设简化蠕变模型。','固定其他参数与简化空腔几何；UHV扩散参数与普通真空并不相同','非独立预测样本数','式3；p8–9比较与参数讨论','初算0.1s对1200s约4数量级差异；改用不同腔半径/中间扩散率改善吻合，不是盲测预测成功。','c008标author_interpretation正确，但其适用性须绑定模型误差与参数调整。',extra=[ev(8,'Thus, the discrepancy obtained is approximately four orders of magnitude.')])],[
    issue('P2','“独立对照”仅表示另一项不同研究/体系；与部分核心论文作者重合，不作为完全独立复制证据。',[],[]),
    issue('P2','强度删失、观察面积和模型误差对研究比较重要，已有重点抽取未覆盖全部限制。',[i+'_c003',i+'_c004',i+'_c008'],[ev(8,'The weakness of the above model is that it does not consider the source of the atoms needed to fill the cavity.')]),
    issue('P2','方法p10将SAM描述为超过20kHz和10nm点分辨率；本轮不擅改，若需定量检测下限应核仪器资料/作者说明。',[],[ev(10,'The SAT examinations were performed in excess of 20 kHz with a point-to-point resolution of 10 nm.')])],
    '保留为历史机理与口径对照；模型定量预测与声学检测下限待补证，不能当无条件HB性能基准。'))

payload={'reviewed_at':datetime.now(timezone.utc).isoformat(),'reviewer':'Codex root; source text + rendered-page inspection','review_kind':'agent_deep_review_not_expert_certification','records':docs}
(OUT/'deep/root.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# 标准、厂商及独立对照组深度复核','', '本文件为审查记录，未改动正式知识库。所有页码为PDF物理页。比较组不是自动认定的独立复现。','']
for d in docs:
    lines += ['## '+d['title'],'',f"`{d['doc_id']}` · {d['group']} · 原件 SHA-256 `{d['source_sha256']}`",'',f"[原PDF](<{d['source_path']}>)；阅读页 {d['reviewed_pages']}；目视图页 {d['visual_pages']}",'']
    for k,v in d['document_assessment'].items():lines += [f'- **{k}**：{v}']
    lines += ['','**使用建议**：'+d['recommended_use'],'']
    for c in d['claims']:
        lines += ['### '+c['claim_id'],'',c['conclusion'],'',f"- 条件：{c['conditions']}",f"- 样本/统计对象：{c['denominator']}",f"- 方法：{c['method']}",f"- 限制：{c['limitations']}",f"- 与现库对照：{c['system_comparison']}",f"- 决定：{c['decision']}",'']
        for e in c['source_evidence']:lines += [f"> PDF p{e['pdf_page']}: {e['quote']}",'']
    if d['issues']:
        lines+=['### 问题与处理建议','']
        for x in d['issues']:lines += [f"- **{x['severity']}**：{x['description']}"]
    lines+=['']
(OUT/'deep/root.md').write_text('\n'.join(lines),encoding='utf-8')
print('root reviews',len(docs),'claims',sum(len(d['claims']) for d in docs))
