# _*_ coding: utf-8 _*_
"""Translation quality gates and response parsing (pure)."""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Tuple

from .formats import extract_json_array, plain_subtitle_text, tag_layout, TagError
from .models import SubtitleCue

PlainTextFn = Callable[[str], str]


CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def no_translation_reason(text: str) -> str:
    plain = plain_subtitle_text(text)
    if not plain:
        return "empty"
    if not re.search(r"[^\W\d_]", plain, re.UNICODE):
        return "symbols_or_music"
    if re.fullmatch(r"[\[(](?:music|instrumental|silence|applause|laughter|"
                    r"sighs?|gasps?|door (?:opens|closes)|gunshots?|音效|音乐)[\])]", plain, re.I):
        return "sound_effect"
    if CJK.search(plain) and not re.search(r"[A-Za-z]", plain):
        return "already_chinese"
    return ""


def has_chinese_cue_coverage(cues: List[SubtitleCue], plain_fn=plain_subtitle_text) -> bool:
    # Existing-track recognition also requires every dialogue cue to be Chinese.
    meaningful = [plain_fn(cue.text) for cue in cues if not no_translation_reason(cue.text)
                  or no_translation_reason(cue.text) == "already_chinese"]
    return bool(meaningful) and all(CJK.search(text) for text in meaningful)


def coverage_failure(ids, reason="译文缺失或未完成中文翻译") -> str:
    return f"{reason}：缺失 {len(ids)} 条，ID {sorted(ids)}"


def validate_translated_cues(source: List[SubtitleCue], translated: List[SubtitleCue],
                             plain_fn=plain_subtitle_text) -> Tuple[bool, str]:
    ids = [cue.cue_id for cue in source]
    if None in ids or len(set(ids)) != len(ids):
        return False, "源 cue ID 缺失或不唯一"
    output_ids = [cue.cue_id for cue in translated]
    if len(set(output_ids)) != len(output_ids) or any(value not in ids for value in output_ids):
        return False, "译文 cue ID 重复或不属于源文件"
    by_id = {cue.cue_id: cue for cue in translated}
    missing = []
    for cue in source:
        target = by_id.get(cue.cue_id)
        if target is None:
            missing.append(cue.cue_id)
            continue
        if (cue.start, cue.end, cue.time_tail) != (target.start, target.end, target.time_tail):
            return False, f"cue ID {cue.cue_id} 时轴被修改"
        try:
            if [m.group() for m in tag_layout(cue.text)] != [m.group() for m in tag_layout(target.text)]:
                raise TagError("标签丢失")
        except TagError:
            missing.append(cue.cue_id)
            continue
        exempt = no_translation_reason(cue.text)
        if exempt:
            if target.no_translation_reason != exempt or target.text != cue.text:
                missing.append(cue.cue_id)
        else:
            text = plain_fn(target.text)
            if not CJK.search(text) or text == plain_fn(cue.text):
                missing.append(cue.cue_id)
    if missing:
        return False, coverage_failure(missing)
    if output_ids != ids:
        return False, "译文 cue ID 顺序与原文不一致"
    return True, ""


def parse_translation_response(content: str) -> Dict[int, str]:
    data = json.loads(extract_json_array(content))
    if not isinstance(data, list):
        raise ValueError("翻译响应不是数组")
    result = {}
    for item in data:
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index in result:
            raise ValueError(f"响应 cue ID 无效或重复：{index}")
        value = item.get("text")
        if not isinstance(value, str):
            raise ValueError(f"cue ID {index} 译文不是字符串")
        result[index] = value.strip()
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


def glossary_key(term: str) -> str:
    """Normalize glossary terms for duplicate detection and user override matching."""
    return re.sub(r"\s+", " ", term or "").strip().lower()


def parse_user_glossary(text: str) -> Dict[str, str]:
    """Parse free-form user glossary textarea (JSON array or term=translation lines)."""
    raw = (text or "").strip()
    if not raw:
        return {}
    parsed: Dict[str, str] = {}
    if raw.startswith("["):
        try:
            for item in json.loads(extract_json_array(raw)):
                if not isinstance(item, dict):
                    continue
                term = str(item.get("term") or item.get("source") or "").strip()
                translation = str(
                    item.get("translation") or item.get("target") or item.get("text") or ""
                ).strip()
                if term and translation:
                    parsed[term] = translation
            return parsed
        except Exception:
            parsed.clear()
    for line in raw.splitlines():
        line = line.strip().strip(",;；")
        if not line:
            continue
        for separator in ["=>", "=", "：", ":"]:
            if separator in line:
                term, translation = line.split(separator, 1)
                term = term.strip()
                translation = translation.strip()
                if term and translation:
                    parsed[term] = translation
                break
    return parsed
