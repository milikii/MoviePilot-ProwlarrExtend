# _*_ coding: utf-8 _*_
from pathlib import Path
from typing import Optional

from .constants import CHI_PATTERNS, ENG_PATTERNS


def normalize_language(value: str) -> str:
    lang = (value or "").strip().lower().replace("_", "-")
    if not lang:
        return ""
    if lang in {"zh", "chi", "zho", "chs", "cmn", "zh-cn", "zh-hans", "cn", "chinese"}:
        return "zh-Hans"
    if lang in {"cht", "zh-tw", "zh-hk", "zh-hant"}:
        return "zh-Hant"
    if lang in {"en", "eng", "english"}:
        return "en"
    return lang


def is_chinese_language(
    language: str,
    title: str = "",
    path: Optional[Path] = None,
) -> bool:
    text = " ".join(filter(None, [language or "", title or "", path.name if path else ""]))
    return normalize_language(language) in {"zh", "zh-Hans", "zh-Hant"} or bool(CHI_PATTERNS.search(text))


def is_english_language(
    language: str,
    title: str = "",
    path: Optional[Path] = None,
) -> bool:
    text = " ".join(filter(None, [language or "", title or "", path.name if path else ""]))
    return normalize_language(language) == "en" or bool(ENG_PATTERNS.search(text))


def language_from_text(text: str, target_language: str = "zh-Hans") -> str:
    if CHI_PATTERNS.search(text or ""):
        return target_language
    if ENG_PATTERNS.search(text or ""):
        return "en"
    return ""
