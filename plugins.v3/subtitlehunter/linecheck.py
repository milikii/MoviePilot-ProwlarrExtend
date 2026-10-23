# _*_ coding: utf-8 _*_
"""Pure Netflix-style subtitle line length / CPS checks."""
from __future__ import annotations

import re
from typing import List, Optional

from .formats import plain_subtitle_text
from .models import SubtitleCue


def parse_subtitle_time(value: str) -> Optional[float]:
    """Parse SRT/ASS timestamps such as 00:01:02,345 or 0:01:02.34."""
    match = re.match(r"^\s*(\d+):(\d{1,2}):(\d{1,2})(?:[,.](\d+))?\s*$", value or "")
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    fraction = match.group(4) or ""
    fraction_seconds = float(f"0.{fraction}") if fraction else 0.0
    return hours * 3600 + minutes * 60 + seconds + fraction_seconds


def cue_duration_seconds(cue: SubtitleCue) -> float:
    start = parse_subtitle_time(cue.start)
    end = parse_subtitle_time(cue.end)
    if start is None or end is None:
        return 0.0
    return max(end - start, 0.0)


def line_length_violations(
    cue: SubtitleCue,
    *,
    chinese_line_limit: int = 16,
    english_line_limit: int = 42,
    max_lines: int = 2,
    max_cps: float = 15.0,
) -> List[str]:
    """Return line count, line length, and CPS violations for one cue."""
    text = plain_subtitle_text(cue.text)
    if not text:
        return []
    lines = [line.strip() for line in text.replace("\\N", "\n").splitlines() if line.strip()]
    if not lines:
        return []

    violations: List[str] = []
    chinese_limit = bool(re.search(r"[\u4e00-\u9fff]", text))
    line_limit = chinese_line_limit if chinese_limit else english_line_limit
    if len(lines) > max_lines:
        violations.append(f"超过两行：{len(lines)} 行")
    for line_no, line in enumerate(lines, start=1):
        if len(line) > line_limit:
            violations.append(f"第 {line_no} 行 {len(line)} 字符，限制 {line_limit}")
    duration = cue_duration_seconds(cue)
    readable_chars = len(re.sub(r"\s+", "", text))
    if duration > 0:
        cps = readable_chars / duration
        if cps > max_cps:
            violations.append(f"CPS {cps:.1f}，限制 {int(max_cps) if max_cps == int(max_cps) else max_cps}")
    return violations
