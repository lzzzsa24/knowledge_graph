"""Hand-selected assertions, with verbatim spans taken from parsed PDF blocks."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
claims = []
blocks = {}
for path in (ROOT / 'parsed').glob('journals_*/elements.jsonl'):
    for line in path.read_text(encoding='utf8').splitlines():
        b = json.loads(line)
        blocks[b['evidence_id']] = b

def ev(doc, block, start=None, end=None):
    b = blocks[f'journals_{doc}:{block}']
    quote = b['normalized_text']
    if start is not None:
        quote = quote[quote.index(start):]
    if end is not None:
        quote = quote[:quote.index(end)]
    return dict(pdf_page=b['pdf_page'], quote=quote.strip(), evidence_id=b['evidence_id'])

def add(doc, statement, material, sample, stage, quantity, value, unit, conditions, evidence, kind='reported_experiment'):
    claims.append(dict(claim_id=f'j{len(claims)+1:03d}', doc_id='journals_'+doc,
        statement=statement, material_system=material, sample_id=sample, stage=stage,
        quantity=quantity, value_text=value, unit=unit, conditions=conditions,
        evidence=evidence, claim_type=kind,
        review_status='agent_extracted_pending_independent_review'))

d='ma15051888'
add(d, '该研究的 (111) 取向纳米孪晶 Cu/SiO2 晶圆在室温预键合后，采用 200 °C、1 h 的键合步骤。',
    'nt-Cu/SiO2', 'wafer-to-wafer nt-Cu/SiO2', 'contact_bonding', 'bonding_temperature_duration', '200 °C; 1 h', '°C; h',
    {'prebond_temperature':'room temperature','prebond_contact_force_kN':75,'prebond_pressure_MPa':1.06,'prebond_vacuum_mbar':'~1e-3','hot_bond_pressure_MPa':None},
    [ev(d,'p2:b6', 'For wafer-to-wafer', 'The elevated temperature')])
add(d, '该研究在 200 °C、1 h 键合后，进一步进行 200 °C、4 h 的后退火。',
    'nt-Cu/SiO2', 'wafer-to-wafer nt-Cu/SiO2', 'post_bond_annealing', 'post_annealing_temperature_duration', '200 °C; 4 h', '°C; h',
    {'preceding_bond_temperature_C':200,'preceding_bond_duration_h':1,'anneal_pressure':None,'anneal_atmosphere':None},
    [ev(d,'p2:b6','For wafer-to-wafer','After the bonding process')])
add(d, 'CMP 后的 Cu 与 SiO2 表面 AFM 均方根粗糙度 Rq 分别为 1.65 nm 和 0.5 nm，均为 6 个样品的平均值。',
    'nt-Cu/SiO2', 'CMP planarized Cu vias and surrounding SiO2', 'material_characterization', 'surface_roughness_Rq', 'Cu: 1.65; SiO2: 0.5', 'nm',
    {'method':'AFM','sample_count_per_surface':6,'processing':'CMP'},
    [ev(d,'p4:b6','To facilitate hybrid bonding','It is worth noting')])
add(d, '该研究经 CMP 后的 nt-Cu 凹陷/回缩深度可控制在 3 nm 以内。',
    'nt-Cu/SiO2', 'CMP planarized Cu vias', 'surface_preparation', 'Cu_dishing_recess', 'within 3', 'nm',
    {'processing':'CMP','metric':'dishing/recess; not roughness'},
    [ev(d,'p4:b6','As shown in Figures 3a')])
add(d, 'CSAM 对一个已键合芯片对的检测显示，超过 95% 的面积良好键合；该值为面积比例。',
    'nt-Cu/SiO2', 'bonded die pair in Figure 5', 'measurement', 'bonded_area_fraction', '>95', '%',
    {'method':'confocal scanning acoustic microscopy','metric':'area fraction; not electrical die yield','bond_temperature_C':200,'post_anneal_temperature_C':200},
    [ev(d,'p6:b2'),ev(d,'p2:b6',end='After the bonding process')])
add(d, '后退火后的交叉条改进 Kelvin 结构中，50 个接头的平均电阻为 1.5 mΩ；10 μm × 8 μm 接触面积对应的比接触电阻为 1.2 × 10^-9 Ω·cm²。',
    'nt-Cu/SiO2', 'crossbar modified Kelvin structures', 'measurement', 'specific_contact_resistance', '1.2e-9', 'Ω·cm²',
    {'method':'four-point probes','joint_count':50,'mean_resistance_mOhm':1.5,'contact_area_um2':80,'post_anneal_temperature_C':200,'post_anneal_duration_h':4},
    [ev(d,'p7:b2','Electrical resistance was measured','So far,'),ev(d,'p2:b6','To strengthen the bonding pair','After the bonding process')])

d='ma18225152'
add(d, '制样流程先在室温键合，再以 300 °C 初次退火完成 Cu/SiO2 混合键合；所引方法段未给出初次退火时间。',
    'Cu/SiO2', 'primary-annealed Cu-Cu joints', 'post_bond_annealing', 'primary_anneal_temperature', '300', '°C',
    {'contact_temperature':'room temperature','duration':None},
    [ev(d,'p2:b7','Subsequently, two wafers','Then,')])
add(d, '二次退火组分别在 350 °C 或 400 °C 保温 30 min，使用流量 100 mL/min、纯度 99.9999% 的氩气保护，并以 5 °C/min 升温。',
    'Cu/SiO2', 'secondary-anneal groups 350C and 400C', 'post_bond_annealing', 'secondary_anneal_conditions', '350 or 400 °C; 30 min', '°C; min',
    {'prior_anneal_temperature_C':300,'atmosphere':'argon','argon_purity_percent':99.9999,'flow_mL_min':100,'pressure':'atmospheric','heating_rate_C_min':5,'cooling':'natural'},
    [ev(d,'p3:b5')])
add(d, 'Cu-Cu 接头横截面纳米压痕测试的最大深度为 300 nm、应变率为 0.05 s^-1；各实验组测 6 个位置，使用 200–300 nm 深度区间的稳定值。',
    'Cu/SiO2', 'Cu-Cu cross-sections, all anneal groups', 'measurement', 'nanoindentation_protocol', '300 nm; 0.05 s^-1; 6 points/group', 'nm; s^-1',
    {'instrument':'G200 Nano Indenter','indenter':'Berkovich','method':'continuous stiffness method','reported_depth_interval_nm':[200,300]},
    [ev(d,'p3:b7')])
add(d, '初次 300 °C 退火的 Cu-Cu 接头硬度为 1.78 GPa；再经 350 °C 或 400 °C 二次退火后分别为 1.67 GPa 和 1.51 GPa。',
    'Cu/SiO2', 'primary 300C; secondary 350C; secondary 400C', 'material_characterization', 'indentation_hardness', '1.78; 1.67; 1.51', 'GPa',
    {'method':'nanoindentation','group_order':['primary 300C','primary 300C + secondary 350C','primary 300C + secondary 400C'],'secondary_duration_min':30,'measurement_depth_nm':[200,300]},
    [ev(d,'p7:b26'),ev(d,'p8:b1'),ev(d,'p3:b5')])
add(d, '通过纳米压痕数据和有限元反演标定所得的屈服强度，初次退火组为 70.52 MPa，350 °C、400 °C 二次退火组分别为 60.54 MPa 和 56.12 MPa；这不是直接拉伸测得的屈服强度。',
    'Cu/SiO2', 'primary 300C; secondary 350C; secondary 400C', 'material_characterization', 'inverse_identified_yield_strength', '70.52; 60.54; 56.12', 'MPa',
    {'method':'nanoindentation-calibrated finite element inverse analysis','software':'ABAQUS 2021','group_order':['primary 300C','secondary 350C','secondary 400C'],'secondary_duration_min':30},
    [ev(d,'p8:b3',end='This indicates'),ev(d,'p4:b15',end='The interfaces'),ev(d,'p7:b17')], 'reported_simulation')
add(d, 'EBSD 得到的平均 Cu 晶粒尺寸在初次 300 °C 退火后为 0.62 μm，在 350 °C 和 400 °C 二次退火后分别为 0.67 μm 和 0.71 μm。',
    'Cu/SiO2', 'primary 300C; secondary 350C; secondary 400C', 'material_characterization', 'mean_Cu_grain_size', '0.62; 0.67; 0.71', 'μm',
    {'method':'EBSD','group_order':['primary 300C','secondary 350C','secondary 400C'],'secondary_duration_min':30},
    [ev(d,'p8:b7',end='These results'),ev(d,'p3:b5')])

d='smsc.202500271'
add(d, '金属化学沉积并经 GPTMS 处理的 Cu/SiO2 芯片在环境条件下接触，随后在 120 °C、2.0 MPa 下热压 2 h；上压板保持室温。',
    'metal-coated Cu/GPTMS-SiO2', 'GPTMS-treated metal-ELD Cu/SiO2 chips', 'contact_bonding', 'thermocompression_conditions', '120 °C; 2 h; 2.0 MPa', '°C; h; MPa',
    {'initial_contact_ambient':'ambient conditions','bottom_plate_temperature_C':120,'top_plate_temperature':'room temperature','surface_treatment':'metal ELD then GPTMS'},
    [ev(d,'p10:b10','The GPTMS-treated','Annealing was subsequently')])
add(d, '处理后的 Cu/SiO2 芯片在热压之后，于氮气中 250 °C 退火 2 h，升温速率为 5 °C/min。',
    'metal-coated Cu/GPTMS-SiO2', 'metal-ELD/GPTMS low-temperature hybrid bonds', 'post_bond_annealing', 'post_anneal_conditions', '250 °C; 2 h', '°C; h',
    {'atmosphere':'nitrogen','heating_rate_C_min':5,'cooling':'natural','previous_contact_C':120,'previous_contact_h':2,'previous_contact_MPa':2.0},
    [ev(d,'p10:b10','The chips were maintained','Also, pristine')])
add(d, 'Au、Pt、Sn 沉积 20 s 的处理组在 250 °C 键合后的芯片剪切强度分别为 5.6、5.2、4.9 MPa；未处理对照在 400 °C 键合时为 2.7 MPa，各组热预算不同。',
    'Au/Pt/Sn-coated Cu/GPTMS-SiO2', 'ELD-Au20, ELD-Pt20, ELD-Sn20, pristine', 'measurement', 'die_shear_strength', 'Au20: 5.6; Pt20: 5.2; Sn20: 4.9; pristine: 2.7', 'MPa',
    {'treated_bond_temperature_C':250,'pristine_bond_temperature_C':400,'ELD_duration_s':20,'method':'die shear','nominal_bond_area_mm2':100,'shear_height_um':100,'shear_speed_um_s':100,'repeat_count':5},
    [ev(d,'p8:b2',end='Figure 8b'),ev(d,'p6:b1',end='Optically transparent'),ev(d,'p10:b1','The bonding strength')])
add(d, 'AFM 测得未沉积 Cu/SiO2 表面的 Ra 约为 1.92 nm，Au20、Pt20、Sn20 组分别为 2.45、2.72、3.34 nm；这里使用 Ra 指标。',
    'Au/Pt/Sn-coated Cu/SiO2', 'pristine, ELD-Au20, ELD-Pt20, ELD-Sn20', 'material_characterization', 'surface_roughness_Ra', 'pristine: ~1.92; Au20: 2.45; Pt20: 2.72; Sn20: 3.34', 'nm',
    {'method':'AFM','ELD_duration_s':20,'metric':'Ra, not optical-profiler Sa or Sq'},
    [ev(d,'p6:b10','Furthermore, the surface roughness','Importantly,'),ev(d,'p6:b1',end='Optically transparent')])
add(d, 'GPTMS 处理后 Cu/SiO2 混合键合芯片的接触角由 69.7° 降为 41.1°。',
    'Cu/GPTMS-SiO2', 'pristine versus GPTMS-treated Cu/SiO2', 'material_characterization', 'contact_angle', '69.7 -> 41.1', 'degree',
    {'surface_modification':'GPTMS silane coating','measurement':'contact angle'},
    [ev(d,'p7:b5','Moreover, contact angle','The OM observation')])
add(d, '作者报告金属沉积与亲水硅烷处理的 Cu/SiO2 混合键合芯片经过 180 °C、280 h 老化后，FE-SEM 中未见 Cu–Cu 及 SiO2–SiO2 界面间隙或空洞；该观察不是电学寿命结论。',
    'metal-coated Cu/GPTMS-SiO2', 'aged metal-ELD/silane-treated hybrid bonding chip', 'reliability_test', 'post_aging_interfacial_morphology', 'no gaps or voids observed after 280 h', None,
    {'aging_temperature_C':180,'temperature_tolerance_C':'-0/+10','duration_h':280,'method':'post-aging FE-SEM','standard_as_reported':'JEDEC 22-A103-B','specific_metal_group':None},
    [ev(d,'p8:b4')])

d='s44172-026-00649-w'
add(d, 'Ti 选择性钝化并平坦化后，Cu 凹陷深度约为 1.4 ± 0.5 nm；Cu 和周围介质的 RMS 粗糙度均小于 0.3 nm，AFM 扫描范围为 2 μm × 2 μm。',
    'Ti-passivated Cu/oxide dielectric', 'passivated CMP-patterned wafer surface', 'material_characterization', 'surface_topography', 'dishing: ~1.4 ± 0.5; RMS roughness: <0.3', 'nm',
    {'method':'AFM','scan_area_um':[2,2],'Ti_thickness_nm':None,'Ti_thickness_disclosure':'proprietary'},
    [ev(d,'p3:b3','AFManalysis','Moreover,'),ev(d,'p3:b3','While the exact Ti thickness','We observed')])
add(d, 'Ti 钝化晶圆经 Ar 等离子体活化后在室温直接键合，再以 250 °C 退火 2 h。',
    'Ti-passivated Cu/oxide dielectric', 'passivated wafer-to-wafer hybrid bonds', 'post_bond_annealing', 'post_anneal_conditions', '250 °C; 2 h', '°C; h',
    {'activation_gas':'argon','contact_temperature':'room temperature','anneal_pressure':None,'anneal_atmosphere':None},
    [ev(d,'p3:b3','Following CMP,','The resulting bonding interface')])
add(d, '电学测试载具的未钝化对照组在键合后以 350 °C 退火 2 h；其热处理条件不同于 250 °C、2 h 的钝化组。',
    'unpassivated Cu/oxide dielectric', 'unpassivated electrical test vehicle controls', 'post_bond_annealing', 'control_post_anneal_conditions', '350 °C; 2 h', '°C; h',
    {'comparator':'Ti-passivated, 250C 2h','test_vehicle':'TSV-connected hybrid bonded daisy chains','anneal_pressure':None},
    [ev(d,'p10:b5','After formation of the bond pads')])
add(d, '跨多个晶圆共 1067 个 die 的电学良率分析中，钝化组约为 97%，未钝化组约为 95%；两组分别经历 250 °C 和 350 °C、2 h 的退火。',
    'Ti-passivated and unpassivated Cu/oxide dielectric', 'electrical test vehicle, passivated versus controls', 'measurement', 'electrical_die_yield', 'passivated: ~97; unpassivated: ~95', '%',
    {'total_die_count_as_reported':1067,'sampling':'across multiple wafers','metric':'electrical die yield','passivated_anneal_C':250,'unpassivated_anneal_C':350,'anneal_h':2},
    [ev(d,'p9:b85'),ev(d,'p10:b5','After formation of the bond pads')])
add(d, 'AFM 线扫描显示未钝化结构的介质边缘塌陷约为 5–6 nm，而钝化表面约为 0.5 nm。',
    'Ti-passivated and unpassivated Cu/oxide dielectric', 'passivated versus unpassivated patterned surfaces', 'material_characterization', 'dielectric_roll_off', 'unpassivated: ~5-6; passivated: ~0.5', 'nm',
    {'method':'AFM line scan','measurement_stage':'before bonding','metric':'dielectric roll-off, not Cu dishing'},
    [ev(d,'p4:b94',end='This local topographical')])
add(d, '该研究在约 700 nm 和 400 nm 节距的细间距结构上实现了共形、Cu 选择性的超薄钝化层；这一结果只证明该尺度的钝化制备。',
    'Ti-passivated Cu/dielectric', 'fine-pitch passivation demonstration structures', 'surface_preparation', 'demonstrated_passivation_pitch', '~700; ~400', 'nm',
    {'demonstration_scope':'selective passivation layer at fine pitch; no electrical yield assigned at these pitches','Cu_pad_diameter_nm_as_reported':'~220'},
    [ev(d,'p8:b11',end='These observations')])

d='nano15201600'
add(d, 'Mo/Cu 表面采用 150 °C 的 Ar + H2S 等离子体硫化；5 min 组的截面 TEM 显示上层 Mo 转为 MoS2 且下层 Cu 保持完整。',
    'Cu/MoS2 with SiCN dielectric', '5 min sulfurized Mo/Cu surface', 'surface_preparation', 'sulfurization_temperature_duration', '150 °C; 5 min', '°C; min',
    {'method':'PECVD Ar + H2S plasma','assessment':'cross-sectional TEM and EDS','initial_Mo_thickness_nm':None,'initial_Mo_thickness_note':'source internally differs: 1.2 nm in results and 2 nm in methods'},
    [ev(d,'p3:b4','The following sulfurization','This low thermal'),ev(d,'p4:b1','At the optimized 5 min','In contrast,')])
add(d, '形成 MoS2 后，SiCN 表面活化使用 250 W、30 s 的 O2 ICP 处理，再浸入去离子水。',
    'Cu/MoS2 with SiCN dielectric', 'MoS2-passivated patterned SiCN coupon wafers', 'surface_preparation', 'oxygen_plasma_activation', '250 W; 30 s', 'W; s',
    {'gas':'oxygen','plasma_mode':'ICP in PECVD system','subsequent_cleaning':'immersion in deionized water','plasma_pressure':None},
    [ev(d,'p5:b6',end='Subsequently,')])
add(d, '经图形对准和加压接触后的 Cu/MoS2/Cu–SiCN 混合键合结构，在 250 °C 烘箱中退火 3 h。',
    'Cu/MoS2/Cu and SiCN/SiCN', 'bonded patterned SiCN coupon wafers', 'post_bond_annealing', 'post_anneal_conditions', '250 °C; 3 h', '°C; h',
    {'contact_pressure':None,'anneal_atmosphere':None,'metal_connection_scope':'MoS2 remains at interface; later voltage-induced Cu filaments provide electrical connectivity'},
    [ev(d,'p5:b6',end='Tensile strength testing')])
add(d, '对 O2 等离子体处理时邻近放置的 SiCN 见证片做 ICP-MS：无金属基线为 9.49 μg/kg，邻近 MoS2 保护 Cu 的组为 9.83 μg/kg，邻近裸露 Cu 的组为 154.11 μg/kg。',
    'SiCN witness coupons adjacent to Cu/MoS2 or exposed Cu', 'pristine baseline; MoS2-protected Cu; exposed Cu', 'measurement', 'Cu_contamination_in_witness_coupon', 'baseline: 9.49; protected: 9.83; exposed: 154.11', 'μg/kg',
    {'method':'ICP-MS','exposure':'adjacent coupons during O2 plasma','measurement_scope':'Cu concentration in witness samples, not Cu film purity or bonded electrical yield'},
    [ev(d,'p6:b9'),ev(d,'p7:b1',end='This demonstrates')])
add(d, '在 Cu–MoS2–Cu 接头的电连接测试中，0.8 V、10 μs 脉冲施加 5 次或以上可形成不可逆导电细丝；3–4 次以内未观察到响应。',
    'Cu/MoS2/Cu', 'post-bond voltage-induced filament junctions', 'measurement', 'irreversible_filament_pulse_threshold', '0.8 V; 10 μs; >=5 pulses', 'V; μs',
    {'function':'voltage-induced electrical connection across MoS2 after bonding','no_response_pulses':'up to three or four'},
    [ev(d,'p7:b1','Furthermore, when the number of pulses','This allowed us'),ev(d,'p9:b1','Second,','(e)')])
add(d, '作者在 Set 操作后的 1、3、7、10 天检查 Cu–MoS2–Cu 导电细丝，报告电连接保持稳定；观测窗口最长为 10 天。',
    'Cu/MoS2/Cu', 'filament retention test after Set', 'reliability_test', 'electrical_connection_retention_window', '1; 3; 7; 10', 'day',
    {'retention_measurement_times_days':[1,3,7,10],'storage_temperature':None,'storage_atmosphere':None,'lifetime_extrapolation':None},
    [ev(d,'p7:b1','Furthermore, these filaments','The controlled synthesis'),ev(d,'p9:b1','(f)')])

d='mi13040537'
add(d, '实验使用 300 mm、厚 775 μm 的硅晶圆，键合面包含 Cu 与氮化硅，Cu pad 节距为 5 μm、宽度为 2 μm。',
    'Cu/silicon nitride', 'patterned 300 mm hybrid bonding wafers', 'material_characterization', 'wafer_and_pad_geometry', 'wafer: 300 mm; pad pitch: 5 μm; pad width: 2 μm', 'mm; μm',
    {'wafer_thickness_um':775,'top_bottom_structure':'same'},
    [ev(d,'p3:b4')])
add(d, '该 Cu/氮化硅晶圆混合键合流程在等离子体活化、去离子水清洗和接触键合后，于 300 °C 退火。',
    'Cu/silicon nitride', 'patterned 300 mm bonded wafers', 'post_bond_annealing', 'post_anneal_temperature', '300', '°C',
    {'duration':None,'atmosphere':None,'previous_steps':['plasma activation','DI water cleaning','wafer contact bonding']},
    [ev(d,'p4:b4')])
add(d, '制备晶圆的键合前 TEM 显示，CMP 后 Cu 凹陷深度小于 25 Å（2.5 nm）。',
    'Cu/silicon nitride', 'fabricated wafer before bonding', 'material_characterization', 'Cu_dishing', '<25', 'Å',
    {'method':'cross-sectional TEM','processing':'after CMP, before bonding','equivalent_nm_upper_bound':2.5},
    [ev(d,'p5:b5',end='Figure 3b')])
add(d, '固定顶/底电极 60/25 W、400/40 kHz 时，O2、Ar、Ar/O2 活化对应的 DCB 键合能分别为 1.69、1.04、1.36 J/m²；氮化硅表面氧化层增厚分别为 33.4、2.7、21.3 Å。',
    'Cu/silicon nitride', 'O2, Ar, Ar/O2 activation groups', 'measurement', 'DCB_bond_energy_by_plasma_gas', 'O2: 1.69; Ar: 1.04; Ar/O2: 1.36', 'J/m²',
    {'method':'double-cantilever-beam','top_power_W':60,'bottom_power_W':25,'top_frequency_kHz':400,'bottom_frequency_kHz':40,'oxide_growth_A':{'O2':33.4,'Ar':2.7,'Ar/O2':21.3},'anneal_temperature_C':300},
    [ev(d,'p6:b4',end='The correlation'),ev(d,'p5:b3',end='Observations'),ev(d,'p4:b4','After wafer bonding')])
add(d, 'O2 活化时，低频功率从 25 W 增至 100 W，DCB 键合能由 1.69 增至 1.84 J/m²；150 W 时为 1.80 J/m²，高频功率固定为 60 W。',
    'Cu/silicon nitride', 'O2 low-frequency power sweep', 'measurement', 'DCB_bond_energy_by_plasma_power', '25 W: 1.69; 100 W: 1.84; 150 W: 1.80', 'J/m²',
    {'gas':'oxygen','high_frequency_power_W':60,'low_frequency_kHz':40,'high_frequency_kHz':400,'method':'double-cantilever-beam','anneal_temperature_C':300},
    [ev(d,'p7:b21'),ev(d,'p8:b1',end='Figure 7 shows'),ev(d,'p5:b1'),ev(d,'p4:b6',end='Plasma activation is'),ev(d,'p5:b3',end='Observations'),ev(d,'p4:b4','After wafer bonding')])
add(d, '研究观察到残余压应力导致凸形翘曲的上晶圆比残余拉应力上晶圆产生更少的边缘空洞；拉应力正翘曲增大时，边缘空洞数增加。',
    'Cu/silicon nitride', 'top wafer residual-stress/warpage experiment', 'measurement', 'edge_void_count_trend', 'fewer with compressive convex top-wafer warpage', None,
    {'method':'warpage-controlled bonding comparison','numerical_void_count':None,'causal_mechanism':'not established; Joule-Thomson explanation is author hypothesis'},
    [ev(d,'p10:b3',end='This was a very interesting')])

d='mi15111332'
add(d, '数值研究的 A 型结构上/下 Cu pad 直径为 3/5 μm；B 型结构两侧均为 4 μm 且错位 1 μm，两类 pad 厚度均为 2 μm。',
    'Cu/SiCN/SiO2 with Ta/TaN barrier', 'FEA type A and type B local models', 'material_characterization', 'simulation_pad_geometry', 'A: 3/5 μm; B: 4/4 μm, offset 1 μm; thickness 2 μm', 'μm',
    {'scope':'model inputs, not fabricated sample measurements','SiCN_thickness_um':0.5,'SiO2_thickness_um':1.7,'barrier_total_thickness_um':0.01},
    [ev(d,'p5:b1','The thicknesses','Figure 3b'),ev(d,'p5:b2',end='To investigate')], 'reported_simulation')
add(d, '有限元退火工况从 25 °C 开始并回到 25 °C，最高温度扫描 200、250、300、350 °C，升降温各 0.5 h，保温 1 h。',
    'Cu/SiCN/SiO2 with Ta/TaN barrier', 'FEA type A and type B anneal sweep', 'post_bond_annealing', 'simulation_thermal_cycle', '200/250/300/350 °C; dwell 1 h; ramp/cool 0.5 h', '°C; h',
    {'scope':'model thermal loading','initial_final_temperature_C':25,'ramp_up_h':0.5,'cool_down_h':0.5,'dwell_h':1,'software':'ANSYS Workbench 2022','model':'2D plane strain'},
    [ev(d,'p6:b3'),ev(d,'p6:b5'),ev(d,'p5:b6',end='To set up')], 'reported_simulation')
add(d, 'A 型模型在 300 °C、5 nm 凹陷工况下，加热段结束时区域 II 点 2 的最大 von Mises 应力为 794 MPa，剥离应力为 680 MPa。',
    'Cu/SiCN/SiO2 with Ta/TaN barrier', 'FEA type A, region II point 2', 'post_bond_annealing', 'simulated_end_heating_stress', 'von Mises: 794; peeling: 680', 'MPa',
    {'anneal_temperature_C':300,'dishing_nm':5,'cycle_phase':'end of heating','interface':'SiCN-to-SiCN','scope':'FEA output'},
    [ev(d,'p10:b4')], 'reported_simulation')
add(d, 'B 型模型在 300 °C、5 nm 凹陷工况下，加热段结束时区域 II 点 2 的最大 von Mises 应力为 675 MPa，剥离应力为 458 MPa。',
    'Cu/SiCN/SiO2 with Ta/TaN barrier', 'FEA type B, region II point 2', 'post_bond_annealing', 'simulated_end_heating_stress', 'von Mises: 675; peeling: 458', 'MPa',
    {'anneal_temperature_C':300,'dishing_nm':5,'misalignment_um':1,'cycle_phase':'end of heating','interface':'SiCN-to-SiCN','scope':'FEA output'},
    [ev(d,'p10:b6',end='The stress level'),ev(d,'p5:b2',end='Both type A')], 'reported_simulation')
add(d, 'A 型模型在 200 °C 退火、3 nm 凹陷时，区域 II 的未键合面积比例仍达 13%。',
    'Cu/SiCN/SiO2 with Ta/TaN barrier', 'FEA type A, region II', 'post_bond_annealing', 'simulated_nonbonding_area_fraction', '13', '%',
    {'anneal_temperature_C':200,'dishing_nm':3,'region':'II','scope':'FEA output; not measured electrical yield'},
    [ev(d,'p9:b2')], 'reported_simulation')
add(d, '300 °C、5 nm 凹陷工况下冷却结束时，A 型模型区域 II 点 1 的剥离拉应力为 626 MPa，点 2 为 -1008 MPa 的压应力；B 型对应值为 467 和 -847 MPa。',
    'Cu/SiCN/SiO2 with Ta/TaN barrier', 'FEA type A/B, region II points 1 and 2', 'post_bond_annealing', 'simulated_end_cooling_peeling_stress', 'A p1:626; A p2:-1008; B p1:467; B p2:-847', 'MPa',
    {'anneal_temperature_C':300,'dishing_nm':5,'cycle_phase':'end of cooling','point1_interface':'SiCN-to-Cu','point2_interface':'SiCN-to-SiCN','sign_convention':'positive tensile; negative compressive','scope':'FEA output'},
    [ev(d,'p13:b6')], 'reported_simulation')

stages={'surface_preparation','contact_bonding','post_bond_annealing','measurement','reliability_test','material_characterization','background'}
for c in claims:
    assert c['stage'] in stages
    assert c['evidence']
    for e in c['evidence']:
        b=blocks[e['evidence_id']]
        assert b['doc_id']==c['doc_id']
        assert b['pdf_page']==e['pdf_page']
        assert e['quote'] in b['normalized_text']
assert len(claims)==42
dest=Path(__file__).with_name('claims_journals.jsonl')
dest.write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in claims),encoding='utf8')
print(f'Wrote {len(claims)} claims to {dest}')
