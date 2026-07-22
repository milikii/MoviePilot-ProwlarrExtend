# _*_ coding: utf-8 _*_
"""Translation quality gates and response parsing (pure)."""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Tuple

from .constants import CHINESE_COVERAGE_THRESHOLD
from .formats import extract_json_array, plain_subtitle_text
from .models import SubtitleCue

PlainTextFn = Callable[[str], str]


def has_chinese_cue_coverage(
    cues: List[SubtitleCue],
    plain_fn: PlainTextFn = plain_subtitle_text,
    threshold: float = CHINESE_COVERAGE_THRESHOLD,
) -> bool:
    meaningful = 0
    chinese = 0
    for cue in cues:
        text = plain_fn(cue.text)
        if not re.search(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff]", text):
            continue
        meaningful += 1
        if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text):
            chinese += 1
    return meaningful > 0 and chinese / meaningful >= threshold


def validate_translated_cues(
    source: List[SubtitleCue],
    translated: List[SubtitleCue],
    plain_fn: PlainTextFn = plain_subtitle_text,
    threshold: float = CHINESE_COVERAGE_THRESHOLD,
) -> Tuple[bool, str]:
    if len(translated) != len(source):
        return False, f"字幕条目数不一致（原文 {len(source)}，译文 {len(translated)}）"
    if [cue.index for cue in translated] != [cue.index for cue in source]:
        return False, "字幕索引与原文不一致"

    for source_cue, translated_cue in zip(source, translated):
        if plain_fn(source_cue.text) and not plain_fn(translated_cue.text):
            return False, f"字幕索引 {source_cue.index} 的译文为空"
    if not has_chinese_cue_coverage(translated, plain_fn=plain_fn, threshold=threshold):
        return False, "中文内容覆盖不足，模型可能未完成翻译"
    return True, ""


def parse_translation_response(content: str) -> Dict[int, str]:
    data = json.loads(extract_json_array(content))
    result: Dict[int, str] = {}
    for item in data:
        index = int(item.get("index"))
        value = str(item.get("text") or "").strip()
        result[index] = value
    return result


def parse_glossary_response(content: str) -> Dict[str, str]:
    data = json.loads(extract_json_array(content))
    result: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        translation = str(item.get("translation") or "").strip()
        if term and translation:
            result[term] = translation
    return result
