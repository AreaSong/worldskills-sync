"""把文件名、标签和栏目解析成技能、届次、类型、阶段、语言。"""

from __future__ import annotations

import re
from dataclasses import dataclass

DOC_TYPES = {
    "technical-description": "技术描述",
    "test-project": "试题",
    "infrastructure-list": "基础设施清单",
    "skill-management-plan": "技能管理计划",
    "correspondence": "通信",
    "resources": "资源",
    "results": "成绩",
    "competition-documents": "竞赛文件",
    "official-document": "官方文件",
    "skill-resource": "技能资料",
    "supporting-documents": "辅助文件",
    "meeting-documents": "会议文件",
    "video": "视频",
    "hall-layout": "场地布局",
    "organizing-guide": "组织指南",
    "marketing-resource": "营销资料",
    "report": "报告",
    "general": "综合",
    "minutes": "纪要",
    "centre-resource": "中心资源",
    "learning-resource": "学习资源",
    "resource-hub": "资源中心附件",
    "wsss": "世界技能标准规范",
    "hse": "健康安全环境",
    "news-resource": "新闻资料",
    "software": "软件",
    "forum-uploads": "论坛上传",
    "museum-archive": "博物馆档案",
    "museum-photo": "博物馆照片",
    "object": "实物资料",
    "archive": "档案馆",
    "museum-record": "博物馆记录",
    "resource-centre": "资源中心",
    "conference-uploads": "会议上传",
    "software-guide": "软件指南",
    "standards-assessment-guide": "标准与评分指南",
    "marking-forms": "评分表",
}

RESOURCE_TYPE_TO_DOC = {
    1: "supporting-documents",
    2: "meeting-documents",
    3: "video",
    4: "technical-description",
    5: "competition-documents",
    6: "hall-layout",
    7: "test-project",
    8: "organizing-guide",
    9: "official-document",
    10: "marketing-resource",
    11: "report",
    12: "correspondence",
    13: "general",
    14: "minutes",
    15: "centre-resource",
    16: "infrastructure-list",
    17: "learning-resource",
    18: "resource-hub",
    19: "wsss",
    20: "hse",
    21: "skill-resource",
    22: "news-resource",
    23: "software",
    24: "forum-uploads",
    25: "museum-archive",
    26: "museum-photo",
    27: "object",
    28: "archive",
    29: "museum-record",
    30: "resource-centre",
    31: "conference-uploads",
    32: "software-guide",
    33: "standards-assessment-guide",
    34: "marking-forms",
}

STAGE_FOLDERS = {
    "actual": "正式",
    "pre": "赛前",
    "proposal": "提案",
    "proposals": "提案",
}

LANG_FOLDERS = {
    "en": "英语",
    "en_us": "英语",
    "en_gb": "英语",
    "zh": "中文",
    "zh_cn": "中文",
    "zh_tw": "中文",
    "de": "德语",
    "es": "西班牙语",
    "fr": "法语",
    "ja": "日语",
    "ko": "韩语",
    "pt": "葡萄牙语",
    "pt_br": "葡萄牙语",
    "fi": "芬兰语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "it": "意大利语",
    "nl": "荷兰语",
    "sv": "瑞典语",
    "pl": "波兰语",
    "th": "泰语",
}

EDITION_NAMES = {
    "WSC2001": "WorldSkills Seoul 2001",
    "WSC2003": "WorldSkills St Gallen 2003",
    "WSC2005": "WorldSkills Helsinki 2005",
    "WSC2007": "WorldSkills Shizuoka 2007",
    "WSC2009": "WorldSkills Calgary 2009",
    "WSC2011": "WorldSkills London 2011",
    "WSC2013": "WorldSkills Leipzig 2013",
    "WSC2015": "WorldSkills São Paulo 2015",
    "WSC2017": "WorldSkills Abu Dhabi 2017",
    "WSC2019": "WorldSkills Kazan 2019",
    "WSC2022": "WorldSkills Shanghai 2022",
    "WSC2022SE": "WorldSkills Competition 2022 Special Edition",
    "WSC2024": "WorldSkills Lyon 2024",
    "WSC2026": "WorldSkills Shanghai 2026",
}

MEMBER_AREA_CODES = tuple(EDITION_NAMES.keys())

EDITION_EVENT_IDS = {
    "WSC2001": 3,
    "WSC2003": 4,
    "WSC2005": 5,
    "WSC2007": 6,
    "WSC2009": 7,
    "WSC2011": 8,
    "WSC2013": 9,
    "WSC2015": 10,
    "WSC2017": 316,
    "WSC2019": 364,
    "WSC2022": 536,
    "WSC2022SE": 594,
    "WSC2024": 579,
    "WSC2026": 611,
}

CMS_SLUG_TO_CODE = {
    "seoul-2001": "WSC2001",
    "st-gallen-2003": "WSC2003",
    "helsinki-2005": "WSC2005",
    "shizuoka-2007": "WSC2007",
    "calgary-2009": "WSC2009",
    "london-2011": "WSC2011",
    "leipzig-2013": "WSC2013",
    "sao-paulo-2015": "WSC2015",
    "worldskills-abu-dhabi-2017": "WSC2017",
    "worldskills-kazan-2019": "WSC2019",
    "shanghai-2022": "WSC2022",
    "special-edition-2022": "WSC2022SE",
    "lyon-2024": "WSC2024",
    "shanghai-2026": "WSC2026",
}

CMS_SECTION_TO_DOC = {
    "technical-descriptions": "technical-description",
    "test-projects": "test-project",
    "infrastructure-lists": "infrastructure-list",
    "skill-management-plans": "skill-management-plan",
    "correspondence": "correspondence",
    "resources": "resources",
    "results": "results",
}

SKILL_TAG_RE = re.compile(r"^Skill\s+(\d+)$", re.I)
CODE_TAG_RE = re.compile(r"^WSC(\d{4})(SE)?$", re.I)
FILENAME_CODE_RE = re.compile(r"\bWSC(\d{4})(SE)?\b", re.I)
TP_RE = re.compile(
    r"WSC(\d{4})(SE)?[_ ]TP([0-9]{1,3}|[A-Z]\d?)(?:[_]([A-Za-z0-9]+))*",
    re.I,
)
TD_RE = re.compile(r"WSC(\d{4})(SE)?[_ ]TD[_ ]?([0-9]{1,3}|[A-Z]\d?)", re.I)
IL_RE = re.compile(r"WSC(\d{4})(SE)?[_ ]IL[_ ]?([0-9]{1,3}|[A-Z]\d?)", re.I)
LANG_SUFFIX_RE = re.compile(r"(?:^|[_\-.])([a-z]{2})(?:_[A-Z]{2})?(?:\.[A-Za-z0-9]+)?$")
LIST_NAME_RE = re.compile(r"^(\d{1,3})\s+(.+)$")


@dataclass
class Classified:
    edition_code: str | None
    edition_name: str
    skill_number: str | None
    skill_name: str | None
    doc_key: str
    stage: str | None
    lang_code: str | None


def pad_skill(number: str | None) -> str | None:
    if number is None:
        return None
    text = str(number).strip()
    if text.isdigit():
        return f"{int(text):02d}"
    return text.upper()


def lang_folder(code: str | None) -> str:
    if not code:
        return "未标注"
    return LANG_FOLDERS.get(code.lower().replace("-", "_"), code)


def doc_folder(key: str) -> str:
    return DOC_TYPES.get(key, "资源")


def edition_name(code: str | None, fallback: str = "未识别届次") -> str:
    if not code:
        return fallback
    return EDITION_NAMES.get(code, fallback)


def member_area_code(code: str | None) -> bool:
    return bool(code) and code in MEMBER_AREA_CODES


KNOWN_LANGS = frozenset(code[:2] for code in LANG_FOLDERS)
NON_LANG_TOKENS = frozenset({"tp", "td", "il", "sm", "ws", "of", "to", "or", "by", "at", "v1", "v2", "v3"})
HOST_TOKEN_RE = re.compile(r"_\d{2}[A-Za-z]{2}(?=_|$|\.)")
LANG_TOKEN_RE = re.compile(r"(?:^|[_\-.])([A-Za-z]{2})(?=$|[_\-.])")


def file_stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def parse_langs(text: str) -> list[str]:
    stem = HOST_TOKEN_RE.sub("", file_stem(text))
    found: list[str] = []
    for match in LANG_TOKEN_RE.finditer(stem):
        code = match.group(1).lower()
        if code in NON_LANG_TOKENS:
            continue
        if code in KNOWN_LANGS:
            if code not in found:
                found.append(code)
    return found


def parse_lang(text: str) -> str | None:
    found = parse_langs(text)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return "mul"


def parse_stage(text: str, tags: list[str] | None = None) -> str | None:
    blob = " ".join([text, *(tags or [])]).lower().replace("-", "_")
    if "proposal" in blob:
        return "提案"
    if re.search(r"\bpre\b|_pre_|pre_competition|pre-competition", blob):
        return "赛前"
    if "actual" in blob:
        return "正式"
    return None


def parse_code_from_text(text: str) -> str | None:
    match = re.search(r"WSC\s*(\d{4})\s*(SE)?", text, re.I)
    if not match:
        return None
    year = match.group(1)
    suffix = "SE" if match.group(2) else ""
    code = f"WSC{year}{suffix}"
    if year == "2022" and suffix:
        return "WSC2022SE"
    return code if member_area_code(code) else code


def parse_skill_from_filename(name: str) -> tuple[str | None, str | None]:
    for regex in (TP_RE, TD_RE, IL_RE):
        match = regex.search(name.replace(" ", "_"))
        if match:
            year, se, skill = match.group(1), match.group(2), match.group(3)
            code = f"WSC{year}{'SE' if se else ''}"
            if year == "2022" and se:
                code = "WSC2022SE"
            return code, pad_skill(skill)
    return None, None


def parse_tags(tags: list[str] | None) -> tuple[str | None, str | None, str | None]:
    code = None
    skill = None
    stage_hint = None
    for tag in tags or []:
        code_match = CODE_TAG_RE.match(tag.strip())
        if code_match:
            year, se = code_match.group(1), code_match.group(2)
            code = f"WSC{year}{'SE' if se else ''}"
            if year == "2022" and se:
                code = "WSC2022SE"
        skill_match = SKILL_TAG_RE.match(tag.strip())
        if skill_match:
            skill = pad_skill(skill_match.group(1))
        lowered = tag.lower()
        if "actual" in lowered:
            stage_hint = "正式"
        elif "pre" in lowered:
            stage_hint = "赛前"
        elif "proposal" in lowered:
            stage_hint = "提案"
    return code, skill, stage_hint


def classify(
    *,
    filename: str = "",
    tags: list[str] | None = None,
    doc_key: str | None = None,
    lang_code: str | None = None,
    edition_hint: str | None = None,
    skill_number: str | None = None,
    skill_name: str | None = None,
) -> Classified:
    tag_code, tag_skill, tag_stage = parse_tags(tags)
    file_code, file_skill = parse_skill_from_filename(filename)
    text_code = parse_code_from_text(filename)
    code = edition_hint or tag_code or file_code or text_code
    if code == "WSC2022SE" or (code == "WSC2022" and "SE" in filename.upper()):
        if "SE" in (filename.upper() + " ".join(tags or []).upper()):
            code = "WSC2022SE"
    number = pad_skill(skill_number) or tag_skill or file_skill
    stage = parse_stage(filename, tags) or tag_stage
    lang = (lang_code or parse_lang(filename) or "").lower().replace("-", "_") or None
    if lang and lang not in {"mul", "und"}:
        lang = lang.split("_", 1)[0][:2]
    key = doc_key or "resources"
    if isinstance(key, int):
        key = RESOURCE_TYPE_TO_DOC.get(key, "resources")
    name = edition_name(code, code or "未识别届次")
    return Classified(
        edition_code=code if member_area_code(code) else code,
        edition_name=name,
        skill_number=number,
        skill_name=skill_name,
        doc_key=key,
        stage=stage if key == "test-project" else None,
        lang_code=lang,
    )


def parse_list_title(title: str) -> tuple[str | None, str | None]:
    match = LIST_NAME_RE.match(title.strip())
    if not match:
        return None, title.strip() or None
    return pad_skill(match.group(1)), match.group(2).strip()
