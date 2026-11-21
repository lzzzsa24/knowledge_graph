"""Agent-authored claims; evidence spans are copied verbatim from the local PDFs.

This is an annotation serialization/validation helper, not an automatic semantic extractor.
"""
from pathlib import Path
import json, re
import pymupdf

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
claims=[]
docs={}
def text(doc,page):
    if doc not in docs: docs[doc]=pymupdf.open(ROOT/'pilot'/'collection'/'imec'/(doc+'.pdf'))
    return ' '.join(docs[doc][page-1].get_text().split())
def ev(doc,page,start,end=None):
    t=text(doc,page)
    try:
        a=t.index(start)
        b=t.index(end,a)+len(end) if end else a+len(start)
    except ValueError as exc:raise ValueError(f'{doc} p{page}: {start!r} .. {end!r}') from exc
    e={'pdf_page':page,'quote':t[a:b]}
    elements=ROOT/'pilot'/'parsed'/doc/'elements.jsonl'
    if elements.exists():
        for line in elements.read_text(encoding='utf-8').splitlines():
            x=json.loads(line)
            if x['pdf_page']==page and e['quote'] in ' '.join(x['text'].split()):
                e['evidence_id']=x['evidence_id'];break
    return e
def add(doc,statement,material,sample,stage,quantity,value,unit,conditions,evidence,kind='reported_experiment',notes=None):
    c=dict(claim_id=f'i{len(claims)+1:03d}',doc_id=doc,statement=statement,material_system=material,
           sample_id=sample,stage=stage,quantity=quantity,value_text=value,unit=unit,conditions=conditions,
           evidence=evidence,claim_type=kind,review_status='agent_extracted_pending_independent_review')
    if notes:c['annotation_notes']=notes
    claims.append(c)

# imec_01: electrical reliability; do not inherit bonding energies cited in its introduction.
d='imec_01';m='Cu/SiCN wafer-to-wafer hybrid bonding';s='w2w_1um_full_pad'
add(d,'该W2W测试结构设计的上、下Cu焊盘直径分别为200和600 nm，节距为1 µm。',m,s,'material_characterization','designed_pad_geometry','top 200 nm; bottom 600 nm; pitch 1 µm',None,
    {'geometry_status':'design values; TEM dimensions are separately reported'},[ev(d,1,'The top and bottom pad','distance of 400 nm.')])
add(d,'TEM测得该样品CMP后的SiCN盖层厚度为98 nm。',m,s,'material_characterization','SiCN_cap_thickness','98','nm',
    {'measurement_method':'TEM','surface_state':'after CMP'},[ev(d,1,'A thin layer','the TEM image.')])
add(d,'界面击穿测试采用controlled-IV，在100–200 °C范围使用1.15、0.26和0.06 V/s三种升压速率。',m,'full_pad_and_bottom_half','measurement','voltage_ramp_rates','1.15 V/s, 0.26 V/s and 0.06 V/s','V/s',
    {'temperature_range':'100 to 200 °C','method':'controlled IV','configurations':['full pad','bottom half']},[ev(d,2,'The breakdown voltage along the bonding interface','voltage.')])
add(d,'作者根据漏电分析判断，在电场高于1 MV/cm时SiCN层导电机制符合Poole-Frenkel发射。',m,s,'measurement','conduction_mechanism','Poole-Frenkel emission',None,
    {'electric_field':'>1 MV/cm','temperature_range':'25 to 300 °C'},[ev(d,4,'To investigate the dominant conduction mechanisms','25 to 300 ◦C.'),ev(d,4,'Therefore, the','higher than 1 MV/cm.')],kind='author_interpretation')
add(d,'作者由Poole-Frenkel拟合得到陷阱深度约0.95 eV。',m,s,'measurement','trap_depth','around 0.95','eV',
    {'model':'Poole-Frenkel','value_origin':'fit intercept'},[ev(d,4,'used to extract the barrier height/trap depth','0.95 eV.')],kind='author_interpretation')
add(d,'幂律寿命外推中，完整焊盘结构在200 °C的Vmax降至11.4 V；这不是直接测得的长期寿命。',m,s,'reliability_test','extrapolated_Vmax','11.4','V',
    {'temperature':'200 °C','pad_pairs':'22500','model':'power-law','lifetime_criterion':'>10 years (extrapolated mean time to failure)'},[ev(d,3,'Assuming a','acceleration factor γ.')],kind='author_interpretation')

# imec_02: die handling and direct dielectric demonstration, not proof of a Cu electrical hybrid joint.
d='imec_02';m='SiCN dielectric on Si; direct dielectric D2W process'
add(d,'SiCN表面CMP后的晶圆先磨薄到50 µm，部分晶圆再用等离子体刻蚀减薄到30、20和10 µm。',m,'thin_die_preparation','surface_preparation','die_thickness_series','50; 30; 20; 10','µm',
    {'substrate':'300 mm Si wafer','bonding_face':'SiCN','thinning_methods':['grinding','plasma etching']},[ev(d,2,'Full thickness 300mm Si wafers','for some of the wafers.')])
add(d,'为进行激光释放，薄晶圆背面涂覆约2 µm的BrewerBond T1107释放层。',m,'thin_die_preparation','surface_preparation','laser_release_layer_thickness','~2','µm',
    {'release_layer':'BrewerBond T1107'},[ev(d,2,'To enable the laser release process step','laser release layer (LRL).')])
add(d,'在50 µm厚、7.2×7.2 mm²刀片切割芯片的部分释放试验中，文中报告155 mJ条件下无芯片掉落，且超过99%仍可拾取。',m,'blade_diced_50um_release','surface_preparation','pickable_fraction','>99','%',
    {'die_thickness':'50 µm','die_dimensions':'7.2×7.2 mm²','reported_laser_fluence':'155 mJ','bonder':'BESI TCadvanced'},[ev(d,3,'The blade diced 50um','carrier wafer, and >99% of the dies can still be picked.')],notes='Source labels this laser fluence but prints mJ without area; retained as reported, not converted to mJ/cm².')
add(d,'取放试验表1中，20 µm厚芯片的损坏数为0/45。',m,'pick_place_20um','measurement','damaged_die_count','0/45',None,
    {'die_thickness':'20 µm','test':'pick and place from glass carrier'},[ev(d,3,'After optimizing the partial release process','carrier wafer coated with a TBM.'),ev(d,4,'Table 1: number of damaged dies','2/24')],notes='Table 1 visually inspected; count is pick/place damage, not electrical hybrid bonding yield.')
add(d,'采用该准备流程的直接介质D2W演示共键合99个芯片，SAM图像分析给出的总无空洞面积为98.5%。',m,'direct_dielectric_99_dies','measurement','void_free_area_fraction','98.5','%',
    {'method':'SAM image analysis','bonded_die_count':'99','target_surface':'blanket SiCN','bond_type':'direct dielectric D2W'},[ev(d,4,'At the same time','for direct dielectric bonding.'),ev(d,5,'In total 99 dies','area of 98.5%.')])

# imec_04: reconstructed wafer-to-wafer process.
d='imec_04';m='Cu/oxide reconstructed wafer-to-wafer hybrid bonding';s='reconstructed_w2w'
add(d,'论文报告晶圆准备优化后，边缘与中心厚度差的偏差指标由88%降至11%。',m,s,'surface_preparation','edge_center_thickness_deviation','88% to 11%','%',
    {'comparison':'pre-optimized condition 1 versus optimized condition'},[ev(d,4,'In the pre-optimized condition 1','after optimization.')],notes='Retains the percentage metric as described by the authors; denominator is not specified in the quoted text.')
add(d,'W2W键合条件3的对准偏差低于200 nm。',m,s,'contact_bonding','alignment_error','below 200','nm',
    {'process_condition':'condition 3','demonstrated_pitch':'2 µm'},[ev(d,4,'Meanwhile, figure 6 (c)','misalignment below 200nm,')])
add(d,'通过CMP优化，重构晶圆在报道的截面检查中实现平坦表面及无脱层或未键合区域的氧化物键合。',m,s,'measurement','oxide_interface_observation','without any instances of delamination or un-bonding areas',None,
    {'method':'cross-sectional SEM','region':'on-chip and inter-die gapfill'},[ev(d,4,'The improvement in erosion','figure 7.'),ev(d,4,'However, through CMP optimization','delamination or un-bonding areas.')])
add(d,'重构W2W演示实现2 µm节距；退火后截面观察未见Cu连接处微空洞或氧化物脱层。',m,s,'measurement','bond_pitch','2','µm',
    {'annealing_temperature':None,'annealing_description':'high-temperature annealing; numerical temperature not reported in the cited passage','method':'cross-sectional imaging'},[ev(d,4,'Figure 8 displays','existing D2W equipment.')])
add(d,'论文报告由100及100000链组成的daisy-chain测试结构导通，未出现开路焊盘。',m,s,'measurement','daisy_chain_continuity','100 and 100,000 chains; without any open pads',None,
    {'test_structure':'daisy-chain TEG','yield_denominator':None},[ev(d,5,'Additionally, daisy','without any open pads.')],notes='No universal electrical yield percentage inferred from the stated continuity result.')

# imec_05: Japanese article; actual test interface is thermal SiO2, not Cu hybrid.
d='imec_05';m='thermal SiO2/SiO2 direct wafer bonding on Si(100)';s='thermal_oxide_w2w'
add(d,'该强度测量研究的接合界面采用100 nm热氧化SiO2膜。',m,s,'surface_preparation','bonding_oxide_thickness','100','nm',
    {'wafer_diameter':'300 mm','substrate':'Si(100)'},[ev(d,1,'接合界面には、厚さ','を用いた。')],notes='Source text also prints Si wafer thickness as 775 mm; that apparent source/encoding anomaly is not normalized or adopted.')
add(d,'未经过接合后热处理的晶圆接受DCB测试，比较手套箱0.007%相对湿度与大气43%相对湿度环境。',m,s,'measurement','DCB_test_relative_humidity','0.007%; 43%','%',
    {'post_bond_annealing':'not performed','methods':['DCB'],'environments':['anhydrous glovebox','atmosphere']},[ev(d,2,'接合後熱処理を行っていないウエハを対象に、','で測定した。')])
add(d,'4PB测得无水环境下中心/中间/侧面区域平均接合能分别为2.88、2.88、2.74 J/m²；大气环境分别为1.82、1.74、1.70 J/m²。',m,s,'measurement','regional_mean_bond_energy','anhydrous 2.88/2.88/2.74; atmospheric 1.82/1.74/1.70','J/m²',
    {'method':'4PB','regions_mm':['0–50','50–100','100–150'],'comparison':'anhydrous versus atmospheric measurement environment'},[ev(d,4,'無水環境下での接合エネルギーは、','も低い値を示した。')],notes='Japanese text and Fig.11 visually inspected; environment effect must not be labeled as a bonding-process improvement.')
add(d,'对接合后热处理得到的高强度样品，4PB试验中所有样品在界面剥离前自身破断，无法测得接合强度。',m,'post_annealed_high_strength','measurement','4PB_measurement_outcome','接合強度測定ができなかった',None,
    {'post_bond_annealing':'performed; temperature/time not specified here','failure_mode':'sample fracture before interface delamination'},[ev(d,4,'続いて、接合後熱処理を行った','なかった。')])

# imec_06: distinguish absolute Cu height from change in height (pumping).
d='imec_06';m='Cu/SiO2 hybrid bonding surface'
add(d,'原位SPM试样采用300 mm Si晶圆，Cu焊盘直径4 µm、节距10 µm。',m,'recessed_and_protruded_surfaces','material_characterization','pad_geometry','diameter 4 µm; pitch 10 µm',None,
    {'wafer_diameter':'300 mm','dielectric':'SiO2'},[ev(d,2,'In this study, 300 mm silicon test wafers','as shown in Fig. 1.')])
add(d,'两种CMP配方分别制得相对SiO2表面初始突起+13 nm和凹陷−9 nm的Cu表面。',m,'recessed_and_protruded_surfaces','surface_preparation','initial_Cu_height','+13 nm; -9 nm','nm',
    {'reference_surface':'SiO2','sample_groups':['protruded','recessed']},[ev(d,2,'By using two different','were generated.')])
add(d,'原位SPM使用2 µN接触力、1 Hz扫描速率和25×25 µm区域；在N2环境中测量至400 °C。',m,'recessed_and_protruded_surfaces','measurement','SPM_contact_force','2','µN',
    {'scan_rate':'1 Hz','scan_area':'25×25 µm','atmosphere':'N2','maximum_temperature':'400 °C','instrument':'Hysitron TI Premier'},[ev(d,2,'An extremely low contact force','different temperatures.'),ev(d,2,'The samples were placed','PID feedback loop.')])
add(d,'初始凹陷Cu样品在400 °C时高度为40 nm，冷却后仍高于SiO2表面9 nm。',m,'initially_recessed','measurement','Cu_height_during_and_after_heating','40 nm at 400 °C; 9 nm after cooling','nm',
    {'initial_Cu_height':'-9 nm','reference_surface':'SiO2','method':'in-situ SPM'},[ev(d,2,'As shown here, Cu in the recessed samples','surface after cooling.')],notes='These are absolute heights relative to SiO2, not total pumping displacements; Fig.4/Table1 visually inspected.')
add(d,'初始突起Cu样品在400 °C时高度为65 nm，冷却至环境温度后为28 nm。',m,'initially_protruded','measurement','Cu_height_during_and_after_heating','65 nm at 400 °C; 28 nm after cooling','nm',
    {'initial_Cu_height':'+13 nm','reference_surface':'SiO2','method':'in-situ SPM'},[ev(d,2,'Similarly, in protruded samples','after cooling to ambient temperature.')],notes='Values describe height relative to SiO2; Fig.4/Table1 visually inspected.')
add(d,'按8个Cu焊盘测量均值，论文估计两类表面在400 °C的最大Cu pumping位移约51±3 nm。',m,'recessed_and_protruded_surfaces','measurement','maximum_Cu_pumping_displacement','51 ± 3','nm',
    {'temperature':'400 °C','number_of_pads_averaged':'8','method':'SPM comparison before and during heating'},[ev(d,3,'As mentioned before, the reported measurement values','and protruded Cu.')],notes='Pumping displacement differs from the absolute Cu heights of 40 and 65 nm; Fig.4/Table1 visually inspected.')

# imec_07: explicit sample identity and no-postanneal comparison.
d='imec_07';m='Cu-TSV/Cu electrode hybrid bonding with ultrathin amorphous Si'
add(d,'该研究TSV的直径和深度分别为5.5和33 µm。',m,'TSV_wafers','surface_preparation','TSV_geometry','diameter 5.5 µm; depth 33 µm',None,
    {'wafer_diameter':'200 mm'},[ev(d,2,'First, TSV wafers of diameter 200 mm','33 μm, respectively.')])
add(d,'样品1由Cu突起4.0 nm的TSV晶圆和4.8 nm电极晶圆构成；样品2对应7.7与29 nm。',m,'sample_1_and_sample_2','surface_preparation','Cu_protrusion_by_sample','sample 1: 4.0/4.8; sample 2: 7.7/29','nm',
    {'pair_order':'TSV wafer / Cu electrode wafer'},[ev(d,2,'The Cu protrusions of TSV wafers','Cu electrode wafer 2.')])
add(d,'两个样品的表面活化混合键合温度为25 °C，压载100 kN，真空低于10^-5 Pa。',m,'sample_1_and_sample_2','contact_bonding','bonding_temperature','25','°C',
    {'pressing_load':'100 kN','vacuum':'less than 10^-5 Pa','interlayer':'ultrathin Si','machine':'Bond Meister MWB-12-ST'},[ev(d,2,'The hybrid bonding conditions were as follows:','Machine Tool Co., Ltd.).')])
add(d,'四端法测得样品1连接电阻约3.5 Ω，该值包含TSV、电极和界面电阻。',m,'sample_1','measurement','connection_resistance','approximately 3.5','Ω',
    {'method':'four-terminal','TSV_Cu_protrusion':'4.0 nm','electrode_Cu_protrusion':'4.8 nm','post_bond_annealing':'not performed'},[ev(d,2,'The Cu protrusions of TSV wafers','Cu electrode wafer 2.'),ev(d,4,'Figure 5 shows','approximately 3.5 Ω.')])
add(d,'样品2未经后退火即可得到低于100 mΩ的连接电阻；该值仍包含TSV、电极和界面贡献。',m,'sample_2','measurement','connection_resistance','less than 100','mΩ',
    {'method':'four-terminal','TSV_Cu_protrusion':'7.7 nm','electrode_Cu_protrusion':'29 nm','post_bond_annealing':'not performed'},[ev(d,4,'Meanwhile, the connection resistance','in bonded sample 2.'),ev(d,4,'As a result, the connection resistance','post-bonding annealing.'),ev(d,5,'On the other hand, when the Cu protrusion','less than 100 mΩ.')])

# imec_09: coupon CMP and activation, not yet a fabricated full hybrid-bond process.
d='imec_09';m='Cu/Ta/SiCN surface preparation for hybrid bonding'
add(d,'所有Cu coupon在实验前用5 wt%柠檬酸处理1 min，以去除原生氧化膜。',m,'Cu_coupons','surface_preparation','citric_acid_concentration','5','wt%',
    {'treatment_time':'1 min','purpose':'native oxide removal'},[ev(d,2,'All the Cu coupons were treated','from the Cu surface.')])
add(d,'2×2 cm coupon CMP实验采用1 psi压力、盘/头转速80/79 rpm，浆料流量30 ml/min。',m,'coupon_CMP','surface_preparation','CMP_pressure','1','psi',
    {'coupon_size':'2×2 cm','platen_speed':'80 rpm','head_speed':'79 rpm','slurry_flow':'30 ml/min','pad':'IC1000 polyurethane'},[ev(d,3,'The polishing experiments were carried out','respectively.'),ev(d,3,'A 0.4 wt% H2O2 solution','30 ml min−1.')])
add(d,'该研究使用N2等离子体活化Cu表面15 s，射频功率100 W，流量35 sccm，工作压力50 Pa。',m,'Cu_activation_coupons','surface_preparation','plasma_duration','15','s',
    {'plasma_gas':'N2','RF_power':'100 W','flow_rate':'35 sccm','working_pressure':'50 Pa'},[ev(d,3,'The Cu samples were activated','50 Pa working pressure.')])
add(d,'原始碱性浆料无H2O2时，Cu、Ta和SiCN去除速率分别为28.2、28.8、84.2 nm/min。',m,'original_alkaline_slurry','surface_preparation','material_removal_rates','Cu 28.2; Ta 28.8; SiCN 84.2','nm/min',
    {'H2O2_concentration':'0 wt%','dilution':'undiluted','test_scale':'coupon'},[ev(d,4,'In the original alkaline slurry without H2O2,','hybrid bonding surfaces.')])
add(d,'浆料以1:2比例加水稀释并含0.4 wt% H2O2时，Cu、Ta和SiCN去除速率分别为20、17、30.5 nm/min。',m,'diluted_alkaline_with_peroxide','surface_preparation','material_removal_rates','Cu 20; Ta 17; SiCN 30.5','nm/min',
    {'slurry_to_water_ratio':'1:2','H2O2_concentration':'0.4 wt%','test_scale':'coupon'},[ev(d,4,'In the slurry diluted threefold','20, 17, and 30.5 nm min−1.')])

# imec_11: PI-PI and Cu/PI procedures have different annealing durations.
d='imec_11'
add(d,'PI-A的PI-PI测试在150 °C、0.35 MPa接触条件下，经250 °C、60 min N2后退火，报告剪切强度超过25 MPa。','PI-A/PI-A','PI_A_PI_PI','measurement','die_shear_strength','over 25','MPa',
    {'bonding_temperature':'150 °C','bonding_pressure':'0.35 MPa','annealing_temperature':'250 °C','annealing_time':'60 min','annealing_atmosphere':'N2','method':'die shear'},[ev(d,3,'PI- A demonstrated','respectively.'),ev(d,2,'These samples were annealed','N2 purged atmosphere.')])
add(d,'Cu/PI-A混合键合试样的接触阶段使用室温载台、200 °C机械臂，施加25 N（0.35 MPa）持续3 s。','Cu/PI-A','hybrid_PI_A_chip','contact_bonding','pressing_duration','3','s',
    {'chuck_temperature':'room temperature','arm_temperature':'200 °C','compressing_force':'25 N','pressure':'0.35 MPa'},[ev(d,5,'After CMP, polished center small chips','annealing at 250 ºC/3 hr.')],notes='Tool setpoints are retained separately; neither is asserted as a measured interface temperature.')
add(d,'Cu/PI-A混合键合试样的后退火为250 °C、3 h，SEM截面中作者报告PI-PI与Cu-Cu连接未见空洞。','Cu/PI-A','hybrid_PI_A_chip','post_bond_annealing','annealing_temperature','250','°C',
    {'annealing_time':'3 hr','inspection':'SEM','bonding_force':'25 N','bonding_duration':'3 s'},[ev(d,5,'The compressing force was','successfully demonstrated.')])
add(d,'Cu/PI-A混合键合样片1在表III三个测点的Cu突起分别为+5.7、+5.4、+8.3 nm。','Cu/PI-A','hybrid_chip_1','surface_preparation','Cu_protrusion','+5.7; +5.4; +8.3','nm',
    {'method':'AFM','table':'Table III','target':'5 nm','measurement_points':['①','②','③']},[ev(d,5,'Table III. Amount of copper protrusion','+3.9 nm')],notes='Table visually inspected. These values conflict with the nearby blanket wording "within the target of 5 nm"; retain reported measurements rather than force all values to 5 nm.')
add(d,'PI-A的B-HAST绝缘测试条件为130 °C、85%RH、3.3 V偏压、300 h，作者报告绝缘性能良好。','PI-A with Cu test electrodes','PI_A_BHAST','reliability_test','test_duration','300','h',
    {'temperature':'130 °C','relative_humidity':'85%','bias_voltage':'3.3 V','outcome':'authors report good insulation reliability; no acceptance threshold specified'},[ev(d,6,'PI-A and PI-B showed good insulation reliabilities','reliability test.'),ev(d,6,'(c) resistivity of PI-A','300 hours,')])
add(d,'PI-B的B-HAST绝缘测试条件为130 °C、85%RH、5 V偏压、100 h，作者报告绝缘性能良好。','PI-B with Cu test electrodes','PI_B_BHAST','reliability_test','test_duration','100','h',
    {'temperature':'130 °C','relative_humidity':'85%','bias_voltage':'5 V','outcome':'authors report good insulation reliability; no acceptance threshold specified'},[ev(d,6,'PI-A and PI-B showed good insulation reliabilities','reliability test.'),ev(d,6,'(d) resistivity of PI-B','100 hours.')])

# imec_12: compare three sequences, keep as-bonded versus annealed measurements separate.
d='imec_12';m='Cu-TSV/Cu electrode hybrid bonding with ultrathin amorphous Si'
add(d,'精密CMP后，Cu与SiO2区域的台阶高度控制在约5–10 nm，SiO2表面粗糙度小于0.5 nm。',m,'prepared_wafers','surface_preparation','Cu_SiO2_step_height','approximately 5–10','nm',
    {'SiO2_surface_roughness':'less than 0.5 nm'},[ev(d,2,'The step height between the Cu','less than 0.5 nm.')])
add(d,'该研究在室温进行表面活化混合键合，压载100 kN、真空低于10^-5 Pa。',m,'all_three_sequences','contact_bonding','pressing_load','100','kN',
    {'temperature':'room temperature','vacuum':'less than 10^-5 Pa','interlayer':'ultrathin Si'},[ev(d,1,'Hybrid bonding was performed at room temperature','ultrathin Si film.'),ev(d,2,'After the precise CMP step','less than 10-5 Pa.')])
add(d,'键合序列3样品在后退火前，四端法测得TSV-Cu电极对电阻约9.9 Ω。',m,'sequence_3_as_bonded','measurement','TSV_Cu_pair_resistance','approximately 9.9','Ω',
    {'sequence':'3','post_bond_annealing':'not yet performed','method':'four-terminal'},[ev(d,4,'Figure 7 shows','approximately 9.9 Ω.')])
add(d,'键合序列3样品经过200 °C、2 h后退火，TSV-Cu电极对电阻降至71 mΩ。',m,'sequence_3_annealed','measurement','TSV_Cu_pair_resistance','71','mΩ',
    {'sequence':'3','annealing_temperature':'200 °C','annealing_time':'two hours','method':'four-terminal'},[ev(d,4,'In contrast, for bonding sequence 3,','two hours.')])
add(d,'STEM-EDX比较中，序列1和2界面残留较多氧，序列3仅含少量氧。',m,'three_sequence_comparison','measurement','interface_oxygen_comparison','sequences 1/2: large amount; sequence 3: small amount',None,
    {'method':'STEM-EDX','sequence_3_distinction':'separate-chamber protection during counterpart wafer cleaning'},[ev(d,3,'Figure 6 shows','a small amount of oxygen.'),ev(d,4,'Conversely, in bonding sequence 3,','Cu electrode wafer was cleaned.')])

if __name__=='__main__':
    for c in claims:
        for e in c['evidence']:
            assert e['quote'] in text(c['doc_id'],e['pdf_page'])
    path=OUT/'claims_imec.jsonl'
    path.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in claims)+'\n',encoding='utf-8')
    from collections import Counter
    print(len(claims),dict(Counter(x['doc_id'] for x in claims)))
