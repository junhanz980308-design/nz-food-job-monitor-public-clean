"""Explainable rules; ambiguous evidence is diagnostic, never a Telegram alert."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from .models import Job

ALERT_TIERS = {"HIGH MATCH", "POSSIBLE MATCH"}
TITLE_STRONG = [
    (r"\bfood (?:scientist|technologist|research|technology|science|formulation|chemist)\b", 48, "食品科学岗位", "title.food"),
    (r"\b(?:npd|new product development|product development|formulation|r\s*(?:&|and)\s*d)\b", 34, "新产品或配方开发", "title.development"),
    (r"\b(?:post[ -]?doctoral|post[ -]?doc|research fellow|research scientist|research associate)\b", 34, "研究类职位", "title.research"),
    (r"\b(?:dairy|plant[ -]?based|flavou?r(?:s|ist)?|aroma|sensory)\b", 25, "乳品、植物基或香气领域", "title.specialism"),
    (r"\b(?:analytical scientist|analytical chemist|ingredients? scientist|applications? (?:technologist|scientist))\b", 34, "分析或原料技术", "title.analytical"),
    (r"\b(?:laboratory scientist|research officer|food innovation)\b", 18, "实验与创新相关", "title.laboratory"),
    (r"\b(?:laboratory|lab) technicians?\b", 24, "实验室技术员", "title.lab_technician"),
]
FOOD = r"\b(?:foods?|beverages?|dairy|milk|yogh?urt|cheese|nutrition|ingredients?|plant[ -]?based|fermentation|meat|grain|cereal|whey|casein)\b"
ANALYTICAL = r"\b(?:gc[×x-]?gc(?:-tofms)?|gc[ -]?ms|lc[ -]?ms|mass spectrom\w*|chromatograph\w*|analytical chem\w*|chemometric\w*|volatiles?|flavou?r\w*|aroma|sensory)\b"
# Additional methods apply only to lab technicians, leaving other role scores unchanged.
LAB_ANALYTICAL = r"\b(?:hplc|uhplc|icp[ -]?ms)\b"
LAB_UNRELATED = (
    r"\b(?:clinical|patholog\w*|ha?ematolog\w*|histolog\w*|dental|patients?|"
    r"medical (?:lab(?:oratory)?|diagnostics?)|veterinary diagnostics?|"
    r"construction (?:lab(?:oratory)?|materials|testing)|concrete|geotechnical|software testing)\b"
)
DOMAIN = [
    (FOOD, 23, "食品与饮料行业", "evidence.food"),
    (r"\b(?:research|develop(?:ment|ing|s)?|r\s*(?:&|and)\s*d|npd|innovation|formulations?|product design)\b", 13, "研究开发工作", "evidence.development"),
    (ANALYTICAL, 18, "食品分析或香气技术", "evidence.analytical"),
    (r"\b(?:hydrocolloids?|stabili[sz]ers?|emulsions?|texture|pilot plant|process development|shelf[ -]life)\b", 12, "配方与工艺技术", "evidence.formulation"),
]
NEGATIVE_TITLE = r"\b(?:sales|account manager|marketing|chef|cook|barista|driver|retail|receptionist|software (?:engineer|developer)|data scientist|cybersecurity|finance|electrical engineer)\b"
UNRELATED = r"\b(?:cyber[ -]?security|cryptograph\w*|astrophysics|cosmology|quantum computing|software development|computer vision|financial modelling|oncology|clinical trials)\b"
# Quality roles are a separate POSSIBLE-match route. Existing scientific title
# rules keep their scores and priority, even when their descriptions mention QA.
QUALITY_ROLE = (
    r"\b(?:quality (?:assurance|control)|qa(?:\s*/\s*qc)?|qc(?:\s*/\s*qa)?|food safety|haccp|food compliance|regulatory affairs)\b"
    r"|\b(?:quality|regulatory|compliance)\s+(?:officer|technician|analyst|technologist|specialist|manager|coordinator|auditor|inspector|advisor|adviser|lead|associate)\b"
    r"|\b(?:food|dairy|beverage|ingredients?)\s+(?:auditor|inspector)\b"
)
QUALITY_DUTIES = (
    r"\b(?:perform|conduct|manage|maintain|implement|monitor|audit|verify|ensure|oversee|coordinate|lead|responsible for)\b"
    r"[^.;\n]{0,60}\b(?:quality assurance|quality control|food safety|haccp|food compliance|regulatory compliance)\b"
)
QUALITY_WRONG_SECTOR = (
    r"\b(?:software|it|construction|civil|building|concrete)\b"
)
QUALITY_SOFTWARE_DUTIES = r"\b(?:software testing|testing software|software qa|test automation|selenium|web application testing|mobile application testing|it testing)\b"
QUALITY_NONFOOD_DUTIES = r"\b(?:pharma(?:ceutical)?|medicines?|drug products?|medical devices?|clinical|construction|concrete)\b"
QUALITY_EXPLICIT_FOOD = (
    r"\b(?:food safety|food quality|food compliance|haccp|dairy quality|beverage quality)\b"
    r"|\b(?:quality assurance|quality control|regulatory compliance)\s+(?:of|for|in|on)\s+(?:the\s+)?(?:food|dairy|beverage|milk|cheese|meat|grain|cereal)\b"
)
# Staff benefits alone do not establish a food-industry remit for a new QA rule.
QUALITY_PERKS = r"\b(?:(?:free|subsidised|subsidized|complimentary) food|food (?:allowance|vouchers?))\b"


RIGHTS_STRICT = (
    r"(?:must|required to|only)\s+(?:be\s+|hold\s+|have\s+)?(?:a\s+)?(?:new zealand|nz)\s+(?:citizen\w*|permanent residen\w*)"
    r"|(?:nz|new zealand)\s+(?:citizens?(?:\s+(?:or|and)\s+permanent residents?)?|permanent residents?)\s+only"
    r"|(?:citizenship|permanent residency)\s+is\s+required"
    r"|must be (?:citizens?|permanent residents?|citizens? or permanent residents?) of (?:new zealand|nz)"
)


@dataclass
class Match:
    score: int
    tier: str
    reasons: list[str]
    rights_flag: str
    salary_flag: str
    signals: list[str] = field(default_factory=list)


def match_job(job: Job) -> Match:
    title = re.sub(r"\s+", " ", (job.title or "").lower()).strip()
    description = re.sub(r"\s+", " ", (job.description or "").lower())[:15000].strip()
    text = title + " " + description
    score, reasons, signals = 0, [], []
    hits = [(points, reason, signal) for pattern, points, reason, signal in TITLE_STRONG if re.search(pattern, title)]
    if hits:
        score += max(x[0] for x in hits) + min(12, (len(hits) - 1) * 6)
        reasons.extend(x[1] for x in sorted(hits, reverse=True)[:2])
        signals.extend(x[2] for x in hits)
    for pattern, points, reason, signal in DOMAIN:
        if re.search(pattern, text):
            score += points
            reasons.append(reason)
            signals.append(signal)
    if not description or description == title:
        signals.append("context.title_only")
    lab_technician = "title.lab_technician" in signals
    # Food benefits alone are not evidence of a food laboratory remit.
    food = bool(re.search(FOOD, re.sub(QUALITY_PERKS, "", text) if lab_technician else text))
    relevant_tech = bool(re.search(ANALYTICAL, text))
    if lab_technician and not relevant_tech and re.search(LAB_ANALYTICAL, text):
        score += 18
        relevant_tech = True
        reasons.append("化学分析技术")
        signals.append("evidence.lab_analytical")
    quality_title = bool(re.search(QUALITY_ROLE, title))
    quality = not hits and (quality_title or bool(re.search(QUALITY_DUTIES, description)))
    quality_food = bool(re.search(FOOD, re.sub(QUALITY_PERKS, "", text)))
    quality_unrelated = quality and bool(
        re.search(QUALITY_WRONG_SECTOR, title)
        or re.search(QUALITY_SOFTWARE_DUTIES, description)
        or (re.search(QUALITY_NONFOOD_DUTIES, text) and not re.search(QUALITY_EXPLICIT_FOOD, text))
    )
    if quality:
        score = min(69, score + 24)
        reasons.append("食品质量、安全与合规职责" if quality_food else "质量或合规岗位，食品行业背景待确认")
        signals.append("title.quality" if quality_title else "evidence.quality_duties")
    lab_unrelated = lab_technician and bool(re.search(LAB_UNRELATED, text))
    unrelated = bool(re.search(NEGATIVE_TITLE, title) or re.search(UNRELATED, text) or quality_unrelated or lab_unrelated)
    if quality_unrelated:
        signals.append("penalty.unrelated_quality")
    if lab_unrelated:
        signals.append("penalty.unrelated_lab")
    if unrelated:
        score -= 70
        signals.append("penalty.unrelated")
    if not hits and not quality and not food:
        score -= 30
        signals.append("penalty.no_relevant_role")
    # NPD/R&D/formulation titles warrant POSSIBLE; generic research/analysis
    # needs food or specific analytical methods. More words alone aren't evidence.
    ambiguous = (bool(hits) and not food and not relevant_tech and (lab_technician or "title.development" not in signals)) or (quality and not quality_food)
    if ambiguous:
        score = min(score, 41)
        signals.append("context.insufficient_relevance")
    if not food or lab_technician:
        score = min(score, 69)  # Generic non-food and technician roles stay below HIGH.
    score = max(0, min(score, 100))
    rights_flag = "未核实；需向雇主确认是否接受海外申请者"
    restricted = bool(re.search(RIGHTS_STRICT, text))
    if restricted:
        rights_flag = "招聘内容可能要求新西兰公民或永久居民，需核实"
        score = min(score, 19)
        signals.append("restriction.citizenship_or_pr")
    elif re.search(r"\b(valid|current|existing)\s+(?:nz\s+|new zealand\s+)?(?:work rights|work visa|right to work|work authorization)\b", text):
        rights_flag = "招聘内容提及现有工作许可要求，请核实"
        signals.append("restriction.existing_work_rights")
    if restricted or unrelated:
        tier = "LOW MATCH"
    elif ambiguous:
        tier = "AMBIGUOUS"
    else:
        tier = "HIGH MATCH" if score >= 70 else "POSSIBLE MATCH" if score >= 42 else "LOW MATCH"
    return Match(score, tier, list(dict.fromkeys(reasons))[:3], rights_flag,
                 job.salary or "薪资未公开或未经核实", signals)
