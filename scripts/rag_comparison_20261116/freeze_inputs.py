"""Freeze the existing corpus and validate the benchmark without running retrieval."""

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sys
import unicodedata
import pymupdf


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pilot"
FROZEN = SOURCE / "benchmarks/rag_comparison_20261116/frozen"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.effective_evidence import load_effective_evidence

# Group connected reports conservatively. Conference presentations reprinting a
# paper remain in the same split even where the lot identity is unresolved.
DEVELOPMENT = {
    "reliability_gao_d2w_2021": ("conference", "dbi-ultra-stacks"),
    "doc_0086822b49e2d5df8658": ("enterprise", "dbi-ultra-stacks"),
    "doc_b92ccd88770cdbf948b5": ("standard", "ipc-tm-650"),
    "journals_ma15051888": ("journal", "nt-cu-sio2-2022-kelvin"),
    "doc_0eebb79bec67e16b8b0c": ("journal", "ti-passivated-direct-cu"),
    "imec_01": ("conference", "imec-w2w-electrical-breakdown-2022"),
    "doc_b9a25e99b026b40039f4": ("conference", "lee-polymer-cmp"),
    "doc_f1bed78ead989436f89d": ("conference", "dbi-ultra-stacks"),
    "doc_266f68275dc3780aa334": ("conference", "dbi-ultra-stacks"),
    "doc_e32cb835d14010241a5a": ("standard", "ipc-tm-650"),
    "doc_0596f8392fed699deb6e": ("enterprise", "set-neo-hb"),
    "reliability_die_edge_2022": ("conference", "dbi-ultra-stacks"),
}
HELDOUT = {
    "journals_ma18225152": ("journal", "secondary-anneal-model"),
    "journals_mi15111332": ("journal", "hybrid-crack-numerical-model"),
    "journals_nano15201600": ("journal", "mos2-passivation"),
    "journals_s44172-026-00649-w": ("journal", "ultrathin-metal-passivation"),
    "journals_smsc.202500271": ("journal", "selective-ultrathin-deposition"),
    "imec_05": ("journal", "dcb-uniformity"),
    "imec_09": ("journal", "cmp-sicn-post-clean"),
    "doc_6ab2d89c8d39ff6800a0": ("journal", "ald-alumina-room-temperature"),
    "doc_21c2dbaca7d64dc44d1d": ("journal", "co-sio2-selective-activation"),
    "doc_95ce2634ffbe629e815d": ("journal", "hydrophilic-cu-alumina"),
    "doc_c9826f3d191fe781ca5c": ("journal", "stack-equivalent-circuit"),
    "doc_169e0ee61c0d0d5497b3": ("journal", "cu-film-thickness"),
    "imec_07": ("conference", "watanabe-tsv-cu-protrusion"),
    "doc_692adec4089440464ddc": ("conference", "self-assembly-d2w"),
    "doc_ebf9d7f451ec83ffa8b4": ("enterprise", "bruker-surface-metrology"),
    "doc_a0dbd400adb244ee27fe": ("enterprise", "evg40-overlay"),
    "doc_8a2612eddf42b4ef03c1": ("patent", "wafer-hybrid-bond-method-patent"),
    "doc_349ac878b635527a5df1": ("patent", "double-hybrid-bonded-dies-patent"),
}
PILOT = [
    "reliability_gao_d2w_2021", "doc_0086822b49e2d5df8658", "doc_b92ccd88770cdbf948b5",
    "journals_ma15051888", "doc_0eebb79bec67e16b8b0c", "imec_01",
]
DOCUMENTS = DEVELOPMENT | HELDOUT
SMOKE = {1, 2, 3, 5, 6, 7, 8, 10, 11, 20}
LEGACY_CASES = SOURCE / "reports/quality_review_20261113/qa_regression_cases.json"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path):
    with path.open(encoding="utf-8-sig") as source:
        return [json.loads(line) for line in source if line.strip()]


def write_json(name, data):
    (FROZEN / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_jsonl(name, data):
    with (FROZEN / name).open("w", encoding="utf-8", newline="\n") as output:
        for row in data:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def digest(path):
    hasher = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def strip_aliases(record):
    if isinstance(record, dict):
        return {
            key: strip_aliases(value)
            for key, value in record.items()
            if key != "query_aliases"
        }
    if isinstance(record, list):
        return [strip_aliases(value) for value in record]
    return record


def production_fingerprint():
    tracked = [
        SOURCE / name for name in (
            "catalog/papers.json", "annotations/claims.jsonl",
            "parsed/evidence.jsonl", "parsed/effective_evidence.jsonl",
            "catalog/evidence_qualifications.json",
        )
    ]
    tracked.extend(path for path in sorted((SOURCE / "indexes").rglob("*")) if path.is_file())
    records = {}
    for path in tracked:
        stat = path.stat()
        file_hash = digest(path)
        assert path.stat().st_mtime_ns == stat.st_mtime_ns, path
        records[path.relative_to(ROOT).as_posix()] = {
            "sha256": file_hash, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        }
    return records


def freeze_corpus():
    inputs = {
        "papers": SOURCE / "catalog/papers.json",
        "claims": SOURCE / "annotations/claims.jsonl",
        "evidence": SOURCE / "parsed/effective_evidence.jsonl",
        "qualifications": SOURCE / "catalog/evidence_qualifications.json",
        "study_connections": SOURCE / "research_map/study_connections.json",
    }
    source_hashes = {name: digest(p) for name, p in inputs.items()}
    papers = {row["doc_id"]: row for row in read_json(inputs["papers"])}
    documents = []
    frozen_papers = []
    pdf_dir = FROZEN / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for doc_id, (group, family) in DOCUMENTS.items():
        paper = papers[doc_id]
        source_pdf = Path(paper["local_path"]).resolve()
        original_sha256 = digest(source_pdf)
        assert original_sha256 == paper["sha256"], doc_id
        pdf = pdf_dir / f"{doc_id}.pdf"
        if not pdf.is_file() or digest(pdf) != original_sha256:
            shutil.copyfile(source_pdf, pdf)
        assert digest(pdf) == original_sha256, doc_id
        with pymupdf.open(pdf) as source_document:
            page_count = source_document.page_count
        assert page_count == paper["page_count"], (doc_id, page_count, paper["page_count"])
        relative_pdf = pdf.relative_to(FROZEN).as_posix()
        frozen_papers.append({**strip_aliases(paper), "local_path": str(pdf)})
        documents.append({
            "doc_id": doc_id,
            "title": paper["title"],
            "source_type": paper["source_type"],
            "category": group,
            "family": family,
            "original_pdf": relative_pdf,
            "original_repository_path": source_pdf.relative_to(ROOT).as_posix(),
            "original_sha256": original_sha256,
            "doi": paper.get("doi"),
            "page_count": page_count,
        })

    assert len(set(d["original_sha256"] for d in documents)) == len(DOCUMENTS)
    assert Counter(d["category"] for d in documents) == {
        "journal": 14, "conference": 8, "enterprise": 4, "standard": 2, "patent": 2
    }
    assert {f for _, f in DEVELOPMENT.values()}.isdisjoint(
        {f for _, f in HELDOUT.values()}
    )
    for connection in read_json(inputs["study_connections"])["connections"]:
        member_ids = {member["doc_id"] for member in connection["members"]}
        assert not (member_ids.intersection(DEVELOPMENT) and member_ids.intersection(HELDOUT))
    evidence = [row for row in read_jsonl(inputs["evidence"]) if row["doc_id"] in DOCUMENTS]
    claims = [row for row in read_jsonl(inputs["claims"]) if row["doc_id"] in DOCUMENTS]
    for document in documents:
        doc_id = document["doc_id"]
        pages = {
            row["pdf_page"] for row in evidence
            if row["doc_id"] == doc_id and row["document_sha256"] == document["original_sha256"]
        }
        assert pages == set(range(1, document["page_count"] + 1)), (doc_id, pages)
    qualifications = read_json(inputs["qualifications"])
    effective = load_effective_evidence(SOURCE, papers=list(papers.values()), claims=claims)
    selected_qualifications = [
        row for row in qualifications["documents"] if row["doc_id"] in DOCUMENTS
    ]
    qualifications["documents"] = [
        row for row in selected_qualifications
        if effective.qualifications(row["doc_id"])
        and all(q["status"] == "active" for q in effective.qualifications(row["doc_id"]))
    ]
    assert qualifications["documents"] == selected_qualifications, "Stale qualifications on a selected document"

    assert source_hashes == {name: digest(p) for name, p in inputs.items()}
    write_json("papers.json", frozen_papers)
    write_jsonl("evidence.jsonl", [strip_aliases(row) for row in evidence])
    write_jsonl("claims.jsonl", [strip_aliases(row) for row in claims])
    write_json("qualifications.json", strip_aliases(qualifications))
    return {
        "schema": 1,
        "documents": documents,
        "pilot_doc_ids": PILOT,
        "document_counts": dict(Counter(d["category"] for d in documents)),
        "page_count": sum(d["page_count"] for d in documents),
        "evidence_rows": len(evidence),
        "control_only_evidence_ids": sorted(
            row["evidence_id"] for row in evidence
            if not any(char.isalnum() for char in row["text"])
            and any(unicodedata.category(char) == "Cc" and not char.isspace() for char in row["text"])
        ),
        "pilot_question_ids": [f"dev-{n:02d}" for n in sorted(SMOKE)],
        "source_hashes": source_hashes,
        "shared_hashes": {
            name: digest(FROZEN / name)
            for name in ("papers.json", "evidence.jsonl", "claims.jsonl", "qualifications.json")
        },
    }


def normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def questions_from_records(split, records, claims):
    questions = []
    gold = []
    for number, item in enumerate(records, 1):
        category, q, claim_ids, facts, denominator, caveats, *options = item
        extras = options[0] if options else {}
        references = []
        for claim_id in claim_ids:
            claim = claims[claim_id]
            references.extend({
                "doc_id": claim["doc_id"],
                "pdf_page": row["pdf_page"],
                "quote": row["quote"],
                "evidence_id": row.get("evidence_id"),
            } for row in claim["evidence"])
        references.extend(extras.get("evidence", []))
        unique_references = list({(r["doc_id"], r["pdf_page"], r["quote"]): r for r in references}.values())
        q_id = f"{split}-{number:02d}"
        doc_ids = sorted({r["doc_id"] for r in unique_references})
        questions.append({
            "id": q_id, "split": split, "question": q, "category": category,
            "doc_ids": doc_ids, "smoke": split == "dev" and number in SMOKE,
        })
        gold.append({
            "id": q_id,
            "answerability": extras.get("answerability", "answerable"),
            "required_facts": facts.split("|"),
            "evidence": unique_references,
            "denominator": denominator,
            "caveats": caveats.split("|") if caveats else [],
            **({"known_case": extras["known_case"]} if "known_case" in extras else {}),
        })
    return questions, gold


def validate(manifest, split_rows, corpus_only):
    known_counts = {"dev": 20, "regression": 10, "heldout": 60}
    pages = {d["doc_id"]: d["page_count"] for d in manifest["documents"]}
    all_questions = set()
    summary = {}
    for split, (questions, gold) in split_rows.items():
        assert len(questions) == len(gold) == known_counts[split]
        allowed_docs = HELDOUT if split == "heldout" else DEVELOPMENT
        summary[split] = dict(Counter(q["category"] for q in questions))
        for q, g in zip(questions, gold, strict=True):
            assert q["id"] == g["id"]
            clean = normalized(q["question"])
            assert clean and clean not in all_questions, q["id"]
            all_questions.add(clean)
            assert q["doc_ids"] and set(q["doc_ids"]).issubset(allowed_docs)
            assert g["required_facts"] and g["evidence"]
            for source in g["evidence"]:
                assert 1 <= source["pdf_page"] <= pages[source["doc_id"]]
                assert source["doc_id"] in q["doc_ids"] and source["quote"]
            if q["smoke"]:
                assert set(q["doc_ids"]).issubset(PILOT)
    assert sum(q["smoke"] for q in split_rows["dev"][0]) == 10
    if not corpus_only:
        assert summary["heldout"] == {
            "fact": 10, "units_conditions": 12, "cross_paper": 12,
            "table_caption": 12, "conflict": 8, "insufficient": 6,
        }
        previous_questions = [
            item["question"] for item in read_json(LEGACY_CASES)["cases"]
        ] + [
            item["question_zh"] for item in read_json(SOURCE / "evaluation/retrieval_questions.json")
        ]
        known = set(map(normalized, previous_questions))
        assert not known.intersection(normalized(q["question"]) for q in split_rows["heldout"][0])
        assert set(HELDOUT) == {
            doc_id for q in split_rows["heldout"][0] for doc_id in q["doc_ids"]
        }, "Every held-out source must be covered"
    return summary


def main():
    if "--fingerprint" in sys.argv[1:]:
        print(json.dumps(production_fingerprint(), indent=2))
        return
    start = production_fingerprint()
    dev_only = "--dev-only" in sys.argv[1:]
    manifest = freeze_corpus()
    claims = {c["claim_id"]: c for c in read_jsonl(FROZEN / "claims.jsonl")}
    split_rows = {"dev": questions_from_records("dev", DEV, claims)}
    if not dev_only:
        split_rows["regression"] = questions_from_records("regression", REGRESSION, claims)
        if "--heldout-spec" in sys.argv:
            spec_arg = Path(sys.argv[sys.argv.index("--heldout-spec") + 1])
            split_rows["heldout"] = questions_from_records("heldout", read_json(spec_arg), claims)
        else:
            split_rows["heldout"] = (
                read_json(FROZEN / "questions.heldout.sealed.json"),
                read_json(FROZEN / "gold.heldout.sealed.json"),
            )
    manifest["question_category_counts"] = validate(manifest, split_rows, dev_only)
    for split, (questions, gold) in split_rows.items():
        suffix = ".sealed" if split == "heldout" else ""
        write_json(f"questions.{split}{suffix}.json", questions)
        write_json(f"gold.{split}{suffix}.json", gold)
    manifest["question_hashes"] = {
        path.name: digest(path)
        for pattern in ("questions.*.json", "gold.*.json")
        for path in sorted(FROZEN.glob(pattern))
    }
    assert start == production_fingerprint(), "Production corpus or indexes changed during freeze"
    manifest["production_fingerprint"] = start
    write_json("manifest.json", manifest)
    print(json.dumps({"document_counts": manifest["document_counts"],
                      "question_category_counts": manifest["question_category_counts"]}))


DEV = [
    ("fact", "Gao 2021的五层试验车是五层TSV裸片吗？Cu-Cu连接在何时形成？", ["r001"],
     "四层TSV芯片加一层盖片|五层完成接触键合之后，整片一次200–250 °C、1小时退火形成贯穿整叠的直接Cu–Cu连接",
     "四层有TSV的die与一层cap die", "区分逐片接触键合和最终统一退火"),
    ("units_conditions", "Gao 2021五层芯片在高压湿热试验里用什么条件，试了多少件，电阻如何？", ["r004"],
     "22件五层模块|121 °C、100% RH、15 psi、168小时|全部通过且平均电阻增加小于1%",
     "22件受试五层模块的平均电阻变化", "限于该批试样与所述试验条件"),
    ("units_conditions", "Adeia焊盘偏移实验中，容许偏移的约80%以哪一段长度为基准？它是设备标称精度吗？", ["doc_0086822b49e2d5df8658_c002"],
     "约80%以焊盘直径为分母|这是偏移量电测仍通过的容限",
     "焊盘直径", "不能当成键合机的对准精度、焊盘间距百分比或产品良率"),
    ("fact", "IPC 2.1.1F制备印制板金相截面建议优先用哪种抛光介质，主要减少哪两种伪影？", ["doc_b92ccd88770cdbf948b5_c004"],
     "优先用金刚石介质|降低金属涂抹和边缘倒圆风险",
     None, "适用范围为印制板金相制样"),
    ("units_conditions", "IPC 2.1.1F的研磨方法B建议的压力与轮速是多少，对应几个多大直径的镶嵌试样？", ["doc_b92ccd88770cdbf948b5_c003"],
     "六个直径38.1 mm（1.5 in）的镶嵌试样|压力351.5 g/cm²（5.0 psi）|300–600 rpm",
     "给定六件38.1 mm镶嵌试样", "这属于磨样设置"),
    ("fact", "2022年(111)纳米孪晶Cu/SiO₂晶圆室温预键合后，升温键合和后退火分别多久？", ["j001", "j002"],
     "室温预键合之后200 °C键合1 h|另做200 °C后退火4 h",
     None, "两个不同工艺步骤"),
    ("units_conditions", "Cu/SiO₂低温键合论文用交叉条Kelvin结构时，接触面积、电阻和比接触电阻各是多少？", ["j006"],
     "10 × 8 µm²|50个接头平均1.5 mΩ|比接触电阻1.2×10⁻⁹ Ω·cm²",
     "交叉条改进Kelvin结构50个接头", "与普通Kelvin结构的6.7 mΩ分开",
     {"known_case": "pilot/evaluation/retrieval_questions.json#q01"}),
    ("units_conditions", "180 °C钝化Cu–Cu键合论文实际试样的Cu与Ti层各多厚，采用什么压力和保温时间？", ["doc_0eebb79bec67e16b8b0c_c001"],
     "Si上300 nm Cu与5–10 nm Ti钝化|面对面180 °C、1.91 MPa、30–50 min",
     None, "条件属于该试样结构"),
    ("table_caption", "Ti钝化铜键合的图1中，SEM看到什么，TEM对两层Ti的位置又显示什么？", ["doc_0eebb79bec67e16b8b0c_c002", "doc_0eebb79bec67e16b8b0c_c003"],
     "Fig.1(a) SEM截面未见空洞或裂纹|Fig.1(b) TEM两Ti层已偏离原始键合界面，之间出现Cu层",
     "图示截面", "局部截面不支持整批无空洞"),
    ("units_conditions", "Hou等IRPS 2022界面击穿实验的受控I–V升压速率和温度窗口是什么？", ["i003"],
     "1.15、0.26、0.06 V/s|100–200 °C|两种焊盘配置",
     "两种焊盘配置和各项测试条件", "温度窗口属于击穿实验"),
    ("insufficient", "Gao 2021的论文是否已经交代了MSL3预处理后全部1000次温循的电阻结果？", ["r005"],
     "45件样品经MSL3预处理，−40到125 °C|文章报告250次首次读数未见电阻率变化|没有完整的1000次结果",
     "45件受试模块；250次是报告的读数点", "1000次是测试要求",
     {"answerability": "insufficient"}),
    ("units_conditions", "Adeia多芯片测试比较了哪三种芯片边长，单一尺寸和多芯片分别怎样布置？", ["doc_0086822b49e2d5df8658_c006"],
     "0.4、1.0、3 mm|单尺寸每个基板一种；混合组三种依次键合到同一晶圆，每个位点三种各一|报告混合组良率未下降",
     "特定多芯片测试载体", "厂商演示未证明任意芯片混装效果"),
    ("fact", "为什么IPC 2.1.1F不建议把抛光态与蚀刻后的分离缺陷做一对一配对？", ["doc_b92ccd88770cdbf948b5_c006"],
     "在规定放大倍数下两种观察状态中所见分离未必一一对应|规范要求时检查抛光态以找内层分离",
     None, "适用范围为多层印制板截面"),
    ("units_conditions", "2022年(111)Cu/SiO₂试验CMP后，Cu与SiO₂各自的AFM Rq是多少，统计了多少样品？", ["j003"],
     "Cu Rq 1.65 nm，SiO₂ Rq 0.5 nm|各为6件平均",
     "每种材料各6个样品", "Rq是均方根粗糙度"),
    ("fact", "Ti钝化实验中，400 °C氮气退火是对什么样品做的？AES深度分析支持了什么扩散解释？", ["doc_0eebb79bec67e16b8b0c_c005"],
     "表面带Ti单个Cu样品|400 °C、60 min N₂退火|部分Cu越过Ti扩散到表面",
     "AES单样品", "区分机理对照与180 °C键合条件"),
    ("units_conditions", "IRPS 2022漏电论文的设计焊盘上、下直径和节距是什么？", ["i001"],
     "上焊盘200 nm，下焊盘600 nm，节距1 µm",
     "文中设计几何", "区分设计数值与实测TEM尺寸"),
    ("table_caption", "Lee的nc-Cu/polymer论文表II中，Slurry C低压和高压各得到多少凹陷？", ["doc_b9a25e99b026b40039f4_c003", "doc_b9a25e99b026b40039f4_c004"],
     "同用Slurry C，低压7 nm，高压14 nm|为AFM凹陷深度",
     "两组表II制程条件", "不能推出任何抛光液都按同一比例变化"),
    ("units_conditions", "IPC-TM-650 2.2.22正文规定金属箔测量的最小面积、最大长宽比和物镜倍率是什么？", ["doc_e32cb835d14010241a5a_c001"],
     "至少200,000 µm²|最大5:1|50×物镜",
     "金属箔试样或物理标准样", "该设置不构成混合键合表面的验收限值"),
    ("units_conditions", "Design A图5里200 °C两小时那块C2W试片，是怎样定义一次菊链电测通过的？", ["doc_266f68275dc3780aa334_c002"],
     "样片29个C2W对全部电测通过|测区50 mm²，31,356个链节全导通才计为通过",
     "29个C2W对；每对串接31,356个链节", "仅该试样及该热史"),
    ("cross_paper", "分别复述Gao五层样品早期和Ti钝化铜接头的温循读数。可以据此判哪个工艺寿命更长吗？", ["r005", "doc_0eebb79bec67e16b8b0c_c007"],
     "Gao的−40到125 °C初次250圈未见电阻率变化|Ti的−55到125 °C 500圈后电阻稳定略降|温度窗口、对象、圈数和测量不同，不支持寿命排序",
     "Gao为45件五层模块；Ti为图6(a)受测结构", "无寿命分布或同工况对照"),
]


def legacy_record(case_id, category, facts, denominator, caveats,
                  *, answerability="answerable", extra_evidence=()):
    case = next(row for row in read_json(LEGACY_CASES)["cases"] if row["case_id"] == case_id)
    return (category, case["question"], case["claim_ids"], facts, denominator, caveats, {
        "known_case": f"{LEGACY_CASES.relative_to(ROOT).as_posix()}#{case_id}",
        "answerability": answerability,
        "evidence": list(extra_evidence),
    })


def page(doc_id, pdf_page, quote, locator, *, visual=False):
    entry = {"doc_id": doc_id, "pdf_page": pdf_page, "quote": quote, "locator": locator}
    if visual:
        entry["verification"] = "pdf_visual"
    return entry


REGRESSION = [
    legacy_record("unit_pad", "units_conditions", "焊盘直径范围2–15 µm",
                  None, "芯片外形8 × 12 mm；原PDF希腊字形不可读作m"),
    legacy_record("unit_die", "units_conditions", "比较50和200 µm厚度|芯片横向8 × 12 mm",
                  "同类测试芯片的两种厚度", "厂商报告两组统计等效；未附统计检验细节"),
    legacy_record("misalignment_conflict", "conflict",
                  "正文写1.82 nm|Fig. 8横向标1.81 µm、纵向1.76 µm|单位与量级存疑，单截面不支持设备对准精度",
                  "一个SEM截面", "保留两侧冲突，不擅自订正唯一值", answerability="conflict",
                  extra_evidence=(
                      page("doc_b9a25e99b026b40039f4", 4, "1.81 µm", "Fig. 8横向图标", visual=True),
                      page("doc_b9a25e99b026b40039f4", 4, "1.76 µm", "Fig. 8纵向图标", visual=True),
                  )),
    legacy_record("gaostack", "units_conditions",
                  "60%为CSAM累计整叠无空洞良率|66%为贯穿五层的中心主TSV菊链电测通过率",
                  "整叠无空洞和电测模块各自的统计对象；>47,000是链节数",
                  "逐层无空洞计数公式只说明逐层良率；p3未单列整叠算法"),
    legacy_record("conditional_yield", "units_conditions",
                  "92%约等于24/26|分母是同批26颗无可见空洞芯片，分子是其中电测通过的24颗|不能视作构建批次总体电测良率",
                  "26颗CSAM未见可见空洞的子群", "无空洞仍可能电学开路",
                  extra_evidence=(page("doc_f1bed78ead989436f89d", 5, "24/26", "Table 1, Build 1; # Die Pass E-test/# Die No void"),)),
    legacy_record("stack_12", "cross_paper",
                  "56%是12层累计无空洞堆叠结果|右图由首层48叠到第12层27叠|Theil图1同一代表照片标为8层，不能作为12层独立图证",
                  "27/48个堆叠，约56%", "图像复用为两页视觉比对；未证明试验批次",
                  extra_evidence=(
                      page("doc_0086822b49e2d5df8658", 16, "Number of Stacks", "柱图第1层为48、第12层为27", visual=True),
                      page("reliability_die_edge_2022", 2, "Photograph of a representative DBI bonded 8-die stack.", "Fig. 1图注"),
                  )),
    legacy_record("no_drop", "conflict",
                  "正文说降到200 °C电良率未降|中心曲线200 °C约94–95%，220和240 °C约100%|图读近似，未给n和误差，无法认定完全没有下降",
                  "中心阵列分条件电测；统计数量未披露", "不将图读近似当作精确数值，保留图文差异",
                  answerability="conflict",
                  extra_evidence=(page("doc_0086822b49e2d5df8658", 17, "1-Center", "图线目读约94–95%/100%/100%", visual=True),)),
    legacy_record("ipc_filters", "conflict",
                  "正文5.2明确不使用滤波|附录A.2.2在50×/0.95 NA、200×1000 µm配置列S-filter 2 µm、F-operation平面校正、L-filter 0.2 mm|文件两段表述须同时呈现",
                  "金属箔测量；附录设置固定", "文件未直接说明两处如何统一；不能擅定一套通用操作",
                  answerability="conflict",
                  extra_evidence=(page("doc_e32cb835d14010241a5a", 4, "F-operation: Plane Correction", "§A.2.2"),)),
    legacy_record("vendor", "insufficient",
                  "1000 UPH是标称空循环吞吐|±0.5 µm@3σ是标称键合后精度|规格受配置、操作和应用影响，不能假定同时满足",
                  "空循环UPH，不是量产合格产出", "厂家脚注明示规格未必同时有效，资料未给联合实测",
                  answerability="insufficient"),
    legacy_record("shared_evidence", "cross_paper",
                  "IWLPC Table 1与另一篇Table I五行相同，24/26、20/20、19/19、13/13、15/15|为共享数据，不计作两次独立验证",
                  "每行分母为无可见空洞的C2W对，分子其中菊链电测通过数",
                  "结论只限重叠表和来源复核记载的SEM，其余条件须分别核对",
                  extra_evidence=(
                      page("doc_f1bed78ead989436f89d", 5, "24/26", "Table 1, 五行数据"),
                      page("doc_266f68275dc3780aa334", 4, "TABLE I.", "200 °C/1 h, 五行数据；原表及study_connections核对"),
                  )),
]


if __name__ == "__main__":
    main()
