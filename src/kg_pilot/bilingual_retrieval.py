"""Deterministic query planning for Chinese questions over technical English.

The glossary translates concepts, never answers. Reviewed bilingual claims may
contribute their *quantity names*, but never their values, quotes or page IDs.
All expansions remain retrieval hints and cannot become evidence.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


def is_crosslingual_query(query: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", query))


@dataclass(frozen=True)
class Concept:
    name: str
    phrases: tuple[str, ...]
    weight: float = 1.0


@dataclass(frozen=True)
class QueryPlan:
    original: str
    intent: str
    concepts: tuple[Concept, ...]
    phrases: tuple[str, ...]
    entity_docs: frozenset[str]
    title_docs: frozenset[str]
    identity_phrases: tuple[str, ...] = ()
    identity_names: tuple[str, ...] = ()
    identifier_docs: frozenset[str] = frozenset()
    publication_years: frozenset[int] = frozenset()


# Reusable terminology, independent of documents, test questions and answers.
# Longer concepts precede their components so one phrase cannot inflate scores.
_GLOSSARY = (
    ("等效电路|等效电路模型", "equivalent circuit|equivalent-circuit model", 2),
    ("图形连通性|图案连通性", "pattern connectivity|connected pattern", 1.6),
    ("图形密度|图案密度|金属占比|铜占比", "pattern density|metal density|metal area fraction|pad density", 1.6),
    ("聚酰亚胺", "polyimide", 1.7),
    ("电抛光|电化学抛光", "electropolishing|electropolished|electrochemical polishing", 1.8),
    ("钌钝化|钌保护层", "ruthenium passivation|Ru passivation", 1.8),
    ("钛钝化|钛保护层", "titanium passivation|Ti passivation", 1.8),
    ("焊盘错位|套刻误差|叠对误差", "pad misalignment|overlay error|misalignment", 1.7),
    ("接触电阻|连接电阻|互连电阻", "contact resistance|connection resistance|interconnect resistance", 2),
    ("击穿电压|击穿场强|介质击穿", "breakdown voltage|breakdown field|dielectric breakdown", 2),
    ("断裂能", "fracture energy|fracture toughness", 2),
    ("键合能|结合能", "bond energy|bonding energy", 2),
    ("界面能", "interfacial energy|interface energy", 2),
    ("剪切强度|抗剪强度", "shear strength|die shear", 2),
    ("键合强度|结合强度", "bond strength|bonding strength", 2),
    ("屈服强度", "yield strength", 2),
    ("接合能", "bond energy|bonding energy", 2),
    ("表面粗糙度|粗糙度", "roughness|rms|rq|ra|sq", 2),
    ("吞吐量|产能|每小时晶圆", "throughput|wafers per hour|wph", 2),
    ("良率|合格率|成功率", "yield|pass rate|success rate", 2),
    ("无空洞|无孔洞", "void free|void-free", 1.4),
    ("累计|累积|整叠|整堆|整体堆叠", "cumulative|overall stack|whole stack", 1.4),
    ("单层|逐层|每层", "per layer|per-layer|each layer|individual layer", 1.2),
    ("多层|堆叠|叠层", "stack|stacked|stacking|multilayer|multi-layer", 1),
    ("芯片到晶圆|芯片对晶圆|芯片级键合", "die to wafer|die-to-wafer|d2w|chip to wafer|c2w", 1.4),
    ("晶圆到晶圆|晶圆对晶圆|晶圆级键合", "wafer to wafer|wafer-to-wafer|w2w", 1.4),
    ("对准误差|对准精度|套刻精度|放置精度", "alignment accuracy|alignment error|overlay|placement accuracy", 2),
    ("对准|套刻", "alignment|overlay", 1.3),
    ("样品尺寸|试样尺寸|芯片尺寸|试样大小", "sample size|die size|chip size|dimensions|diced", 2),
    ("尺寸|大小", "size|dimensions|diameter", 1),
    ("节距|间距", "pitch|spacing", 2),
    ("焊盘直径|直径", "diameter|pad diameter", 2),
    ("厚度|多厚", "thickness|thick", 2),
    ("硅通孔|通孔", "tsv|through silicon via|via", 1.7),
    ("凹陷|凹蚀", "dishing|recess|recessed", 2),
    ("凸起|突起|膨出", "protrusion|extrusion|protrude", 2),
    ("平坦化|抛光", "planarization|polishing|cmp", 1.3),
    ("塌边|边缘塌陷", "roll off|roll-off|edge roll", 2),
    ("热膨胀|膨胀失配", "thermal expansion|expansion mismatch|coefficient of thermal expansion|cte", 2),
    ("选择性化学镀|化学镀", "electroless|selective deposition", 1.8),
    ("清洗|清洁|洁净", "cleaning|clean|cleanliness", 1.3),
    ("活化|激活", "activation|activated", 1.3),
    ("等离子", "plasma", 1.5),
    ("氧等离子|氧气|氧处理", "oxygen|o2", 1.3),
    ("氮等离子|氮气|氮处理", "nitrogen|n2", 1.3),
    ("低频", "low frequency|low-frequency|lf", 1.3),
    ("射频", "radio frequency|rf", 1.3),
    ("功率", "power", 1.8),
    ("钝化|抗氧化|氧化抑制", "passivation|oxidation suppression|oxidation prevention", 1.5),
    ("氧化", "oxidation|oxide", 1),
    ("污染|残留", "contamination|residue", 1.5),
    ("颗粒", "particle|particles", 1.5),
    ("切割|划片", "dicing|diced|singulation", 1.5),
    ("拾取|拾放|搬运", "pick|handling|placement", 1.3),
    ("键合波", "bonding wave|bond wave", 2),
    ("退火", "anneal|annealing|annealed", 1.5),
    ("晶粒长大|再结晶", "grain growth|recrystallization", 2),
    ("低温", "low temperature|low-temperature", 1.3),
    ("室温", "room temperature|room-temperature", 1.3),
    ("温度循环|温循|热循环", "temperature cycling|thermal cycling|tct", 2),
    ("温度", "temperature", 1),
    ("时间|时长|多久", "time|duration", .7),
    ("压力|载荷", "pressure|load", 1.3),
    ("空洞|孔洞|未键合", "void|voids|unbonded", 1.6),
    ("裂纹|开裂", "crack|cracking|fracture", 1.6),
    ("分层|脱层", "delamination", 1.8),
    ("热应力|残余应力", "thermal stress|residual stress", 2),
    ("热导|导热率", "thermal conductivity|thermal conductance", 2),
    ("热阻", "thermal resistance", 2),
    ("热传导", "heat transfer|thermal conduction", 1.5),
    ("翘曲|形变", "warpage|bow|deformation", 1.7),
    ("漏电", "leakage|leakage current", 2),
    ("导电通道|导电细丝", "conductive pathway|conductive filament|conduction path", 1.8),
    ("导电", "conductive|conductivity|conduction", 1),
    ("元素分布|元素映射", "elemental distribution|elemental mapping|element mapping|eds|edx", 1.8),
    ("脉冲", "pulse|pulsed", 1.3),
    ("电阻率", "resistivity", 2),
    ("电阻", "resistance", 1.7),
    ("电连接|电接触", "electrical connection|electrical contact", 1.5),
    ("导通|连续性", "continuity|electrical connection|conductivity", 1.7),
    ("高温存储|高温储存", "high temperature storage|hts", 2),
    ("湿热|高压蒸煮", "humidity|damp heat|hast|autoclave", 1.5),
    ("干燥|无水", "dry|anhydrous|moisture free|humidity", 1.4),
    ("空气|环境湿度", "air|ambient|humidity", 1.1),
    ("四点弯曲|四点弯", "four point bending|four-point bending|4pb", 2),
    ("纳米压痕", "nanoindentation|nano indentation", 2),
    ("有限元|数值模拟", "finite element|finite element method|FEM|simulation|numerical", 1.5),
    ("剥离应力", "peeling stress|peel stress", 2),
    ("中心|边缘", "center|central|edge", .8),
    ("可靠性|寿命", "reliability|lifetime", 1.3),
    ("红外", "infrared|ir", 1.5),
    ("声学|超声|声显微", "acoustic|ultrasonic|sam|csam|usam", 1.8),
    ("测量方法|检测方法|表征方法|怎么测|如何测|用什么方法|测的", "measurement|measured|characterization|inspection|imaging|microscopy", .8),
    ("适用范围|适用对象|适用于|覆盖范围", "scope|applies to|applicable|application", 1.6),
    ("限制|局限", "limitation|limited|limit", .8),
    ("样品|试样", "sample|specimen", .6),
    ("预键合", "pre bonding|pre-bonding|prebonding|prebond", 1.5),
    ("压板", "platen|plate|chuck", 1),
    ("晶圆", "wafer|wafers", .5),
    ("芯片|芯粒", "die|chip|chiplet", .5),
    ("混合键合", "hybrid bonding", .3),
    ("键合|结合", "bonding|bonded|bond", .3),
)
_STOP = set("the and for of in on a an inc ltd limited corporation company international institute university laboratory national physical electronics council technology technologies instruments hybrid bonding copper silicon semiconductor materials research application note guide measurement good practice no rev issue".split())
_QUERY_STOP = set("what which how in the for and with of a to is was were at from did under are does this that after before reported paper study using used compare between than into please result results".split())


def title_documents(question: str, papers: dict) -> tuple[set[str], str]:
    """Shared exact-title matcher, retaining the previous QA contract."""
    intent = normalized(question)
    candidates = []
    for doc_id, paper in papers.items():
        parts = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized(paper.get("title") or ""))
        if len(parts) < 4 or len("".join(parts)) < 24:
            continue
        pattern = r"(?<![a-z0-9])" + r"[\W_]*".join(map(re.escape, parts)) + r"(?![a-z0-9])"
        candidates.extend((m.start(), m.end(), doc_id) for m in re.finditer(pattern, intent))
    spans, matched = [], set()
    for start, end, doc_id in sorted(candidates, key=lambda x: (-(x[1] - x[0]), x[0], x[2])):
        if any(start >= a and end <= b and (start, end) != (a, b) for a, b in spans):
            continue
        matched.add(doc_id)
        spans.append((start, end))
    for start, end in sorted(set(spans), reverse=True):
        intent = intent[:start] + " " + intent[end:]
    return matched, intent


def _identity_documents(question: str, papers: dict) -> tuple[set[str], tuple[str, ...]]:
    aliases: dict[str, set[str]] = {}
    for doc_id, paper in papers.items():
        provenance = paper.get("provenance") or {}
        publisher = str(paper.get("publisher") or provenance.get("publisher") or "")
        values = {word for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", publisher)
                  if normalized(word) not in _STOP and (len(word) >= 4 or word.isupper() and len(word) >= 2)}
        full_name = re.sub(r"\([^)]*\)", "", publisher).strip()
        if len(full_name.split()) >= 2:
            values.add(full_name)
        number = str(paper.get("document_number") or provenance.get("document_number") or "")
        if re.search(r"\d", number):
            values.add(number)
        if paper.get("doi"):
            values.add(str(paper["doi"]))
        # Surnames come from the catalog, never a fixed list of known papers.
        for author in paper.get("authors") or []:
            if isinstance(author, dict):
                surname = str(author.get("family") or "")
            else:
                surname = str(author).split(",")[0] if "," in str(author) else str(author).split()[-1] if str(author).split() else ""
            if len(surname) >= 3 and normalized(surname) not in _STOP:
                values.add(surname)
        for alias in values:
            aliases.setdefault(normalized(alias), set()).add(doc_id)
    result, matched = set(), []
    for alias, docs in aliases.items():
        if len(docs) > max(3, len(papers) // 5):
            continue
        pieces = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", alias)
        if pieces and re.search(r"(?<![a-z0-9])" + r"[\s.\-–_/]*".join(map(re.escape, pieces)) + r"(?![a-z0-9])", question, re.I):
            result.update(docs)
            matched.append(alias)
    return result, tuple(matched)


def _identifier_documents(question: str, papers: dict) -> tuple[set[str], tuple[str, ...]]:
    """Rare product/standard identifiers can live only in catalog titles."""
    aliases: dict[str, set[str]] = {}
    for doc_id, paper in papers.items():
        title = str(paper.get("title") or "")
        # At least two letters and two digits exclude D2W, SiO2 and similar
        # ubiquitous scientific abbreviations without a manufacturer list.
        identifiers = set(re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{2,}[-_]?\d{2,}[A-Za-z0-9-]*(?![A-Za-z0-9])", title))
        provenance = paper.get("provenance") or {}
        number = str(paper.get("document_number") or provenance.get("document_number") or "")
        if re.search(r"\d", number):
            identifiers.add(number)
        for identifier in identifiers:
            aliases.setdefault(normalized(identifier), set()).add(doc_id)
    matched, result = [], set()
    for alias, docs in aliases.items():
        if len(docs) > max(2, len(papers) // 20):
            continue
        tokens = english_tokens(alias)
        if tokens and re.search(r"(?<![a-z0-9])" + r"[\s\-_/.]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])", normalized(question)):
            matched.append(alias)
            result.update(docs)
    return result, tuple(matched)


def english_tokens(text: str) -> tuple[str, ...]:
    # Retain sequence and repetitions for phrase matching. Stop-word removal
    # or deduplication here would create adjacencies absent from the source.
    return tuple(re.findall(r"[a-z][a-z0-9]*|\d+(?:\.\d+)?", normalized(text)))


def english_terms(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(w for w in english_tokens(text)
                               if w not in _QUERY_STOP and (len(w) > 1 or w.isdigit())))


def _quantity_bridge(intent: str, claims) -> list[Concept]:
    """Learn a narrow Chinese label -> English quantity bridge from records.

    Only a sufficiently specific quantity label matching the user's text may
    contribute terms. General overlaps in statements or conditions are avoided.
    No numeric values, measurement settings or evidence quotes are expanded.
    """
    found = []
    for claim in claims:
        quantity = str(claim.get("quantity") or "")
        labels = re.findall(r"[\u3400-\u9fff]{2,}", quantity)
        matched = [label for label in labels if len(label) >= 3 and label in intent]
        if not matched:
            continue
        tokens = tuple(t for t in english_terms(quantity.replace("_", " ")) if not t.isdigit())
        if tokens:
            found.append(Concept(max(matched, key=len), tokens[:6], 1.0))
        if len(found) >= 4:
            break
    return found


def plan_query(query: str, papers=(), claims=()) -> QueryPlan:
    papers = papers if isinstance(papers, dict) else {p["doc_id"]: p for p in papers}
    # Latest explicit identity supersedes older conversation identities. Without
    # a new identity, retain user history for ordinary follow-up pronouns.
    lines = [line for line in query.splitlines() if line.strip()]
    latest = lines[-1] if lines else query
    latest_titles, latest_intent = title_documents(latest, papers)
    latest_entities = _identity_documents(latest_intent, papers)[0] | _identifier_documents(latest_intent, papers)[0] | latest_titles
    scope_query = latest if latest_entities else query
    title_docs, intent = title_documents(scope_query, papers)
    entities, identity_phrases = _identity_documents(intent, papers)
    identifier_docs, identifiers = _identifier_documents(intent, papers)
    entities |= identifier_docs
    identity_phrases = tuple(dict.fromkeys((*identity_phrases, *identifiers)))
    entities |= title_docs
    years = frozenset(int(value) for value in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", intent)) if entities else frozenset()
    identity_names = set()
    for paper in papers.values():
        for author in paper.get("authors") or []:
            if isinstance(author, dict):
                name = " ".join(str(author.get(field) or "") for field in ("given", "family")).strip()
            else:
                name = str(author)
                if "," in name:
                    family, given = name.split(",", 1)
                    name = given.strip() + " " + family.strip()
            tokens = english_tokens(name)
            if len(tokens) > 1 and tokens[-1] in identity_phrases:
                identity_names.add(" ".join(tokens))
    remaining = normalized(intent)
    concepts = []
    for chinese, english, weight in _GLOSSARY:
        # Element-plus-process modifiers can coexist with the process itself.
        search_text = normalized(intent) if chinese.startswith(("氧等离子", "氮等离子")) else remaining
        matches = [alias for alias in chinese.split("|") if alias in search_text]
        if matches:
            concepts.append(Concept(max(matches, key=len), tuple(english.split("|")), weight))
            for match in sorted(matches, key=len, reverse=True):
                remaining = remaining.replace(match, " ")
    # Layer counts are a linguistic rule, not a special stack/question case.
    digits = {"二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    words = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    for count in re.findall(r"([二两三四五六七八九十]|\d+)\s*层", intent):
        number = digits.get(count, int(count) if count.isdigit() else 0)
        if number:
            variants = [f"{number} layer", f"{number} die"]
            if number in words:
                variants += [f"{words[number]} layer", f"{words[number]} die"]
            concepts.append(Concept(f"{number}层", tuple(variants), 1.4))
    concepts.extend(_quantity_bridge(normalized(intent), claims))
    # Explicit Latin material names, methods and identifiers survive translation.
    for token in english_terms(intent):
        if token.isdigit() and int(token) in years:
            continue  # Publication years are metadata, never passage relevance.
        if not any(token in {t for p in c.phrases for t in english_terms(p)} for c in concepts):
            concepts.append(Concept(token, (token,), 1.0))
    concepts = concepts[:32]
    phrases = tuple(dict.fromkeys(p for c in concepts for p in c.phrases))[:96]
    # Publication-year hints are metadata identities only when a source has
    # already been named. Neither years in passages nor query result values
    # can create an identity match.
    return QueryPlan(query, intent, tuple(concepts), phrases, frozenset(entities), frozenset(title_docs), identity_phrases,
                     tuple(sorted(identity_names)), frozenset(identifier_docs), years)


def concept_coverage(plan: QueryPlan, text: str) -> tuple[float, list[str]]:
    if not plan.concepts:
        return 0.0, []
    joined = " " + " ".join(english_tokens(text)) + " "
    hits = [c for c in plan.concepts if any(" " + " ".join(english_tokens(p)) + " " in joined for p in c.phrases)]
    total = sum(c.weight for c in plan.concepts)
    return sum(c.weight for c in hits) / total, [c.name for c in hits]
