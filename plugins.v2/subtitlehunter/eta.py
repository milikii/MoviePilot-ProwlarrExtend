# _*_ coding: utf-8 _*_
from math import ceil
from typing import Dict


def estimate_eta_seconds(
    video_count: int,
    total_chars: int,
    translation_profile: str = "quality",
    batch_chars: int = 9000,
    parallel_batches: int = 1,
) -> int:
    """Estimate workflow duration from total subtitle chars, profile and concurrency.

    total_chars already covers all scanned subtitles for the job, so do not
    multiply by video_count again.
    """
    if video_count <= 0:
        return 0
    profile_multiplier = {
        "fast": 1,
        "standard": 2,
        "quality": 3,
    }.get(translation_profile, 3)
    extra_stages = 1.0
    if total_chars <= 0:
        batches = max(video_count, 1)
        profile_multiplier = 0.2
        extra_stages = 0.1
    else:
        batches = max(ceil(total_chars / max(batch_chars, 1)), 1)
    workers = max(parallel_batches, 1)
    return int(ceil(batches * (profile_multiplier + extra_stages) * 45 / workers))


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "未知"
    if seconds < 60:
        return f"约 {seconds} 秒"
    if seconds < 3600:
        return f"约 {ceil(seconds / 60)} 分钟"
    hours = seconds // 3600
    minutes = ceil((seconds % 3600) / 60)
    if minutes >= 60:
        hours += 1
        minutes = 0
    if minutes == 0:
        return f"约 {hours} 小时"
    return f"约 {hours} 小时 {minutes} 分钟"


def empty_stage_timings() -> Dict[str, float]:
    return {}
