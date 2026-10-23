# _*_ coding: utf-8 _*_
"""Stable failure codes and stage names for SubtitleHunter observability."""
from enum import Enum


class Stage(str, Enum):
    SCAN = "scan"
    RENAME = "rename"
    EXTRACT = "extract"
    TRANSLATE = "translate"
    GLOSSARY = "glossary"
    COMPRESS = "compress"
    WRITE = "write"
    AI_TEST = "ai_test"


class FailureCode(str, Enum):
    OK = "ok"
    CANCELLED = "cancelled"
    TARGET_MISSING = "target_missing"
    NO_VIDEO = "no_video"
    NO_ENGLISH_SOURCE = "no_english_source"
    AI_CONFIG = "ai_config"
    AI_HTTP = "ai_http"
    AI_PARSE = "ai_parse"
    TRANSLATE_QUALITY = "translate_quality"
    EXTRACT_FAILED = "extract_failed"
    WRITE_FAILED = "write_failed"
    FFMPEG = "ffmpeg"
    UNKNOWN = "unknown"


def map_error_message(message: str) -> str:
    text = (message or "").lower()
    if "取消" in (message or "") or "cancel" in text:
        return FailureCode.CANCELLED.value
    if "ai" in text and ("配置" in (message or "") or "不可用" in (message or "")):
        return FailureCode.AI_CONFIG.value
    if "http" in text or "api" in text:
        return FailureCode.AI_HTTP.value
    if "中文内容覆盖不足" in (message or "") or "条目数不一致" in (message or "") or "索引" in (message or ""):
        return FailureCode.TRANSLATE_QUALITY.value
    if "提取" in (message or "") or "ffmpeg" in text or "ffprobe" in text:
        return FailureCode.EXTRACT_FAILED.value if "提取" in (message or "") else FailureCode.FFMPEG.value
    if "不存在" in (message or ""):
        return FailureCode.TARGET_MISSING.value
    if "未发现视频" in (message or ""):
        return FailureCode.NO_VIDEO.value
    if "英文" in (message or "") and "源" in (message or ""):
        return FailureCode.NO_ENGLISH_SOURCE.value
    if "写入" in (message or "") or "write" in text:
        return FailureCode.WRITE_FAILED.value
    return FailureCode.UNKNOWN.value
