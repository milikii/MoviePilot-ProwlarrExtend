# _*_ coding: utf-8 _*_
"""Subtitle parse/render and text helpers (pure, no plugin state)."""
from __future__ import annotations

import re
from typing import List, Tuple

from .constants import SRT_TIME
from .models import SubtitleCue


def plain_subtitle_text(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", text or "")
    cleaned = re.sub(r"\{[^}]*}", "", cleaned)
    cleaned = cleaned.replace("\\N", "\n")
    return cleaned.strip()


def is_sentence_boundary(text: str) -> bool:
    cleaned = plain_subtitle_text(text)
    return bool(re.search(r"(?:\.{3}|[.!?。！？]|…+)[\"'）)\]\}”’]*\s*$", cleaned))


def extract_json_array(content: str) -> str:
    """Extract the first JSON array from a model response, allowing fenced JSON."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def parse_srt(content: str) -> List[SubtitleCue]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues = []
    for block_no, block in enumerate(re.split(r"\n\s*\n", normalized), start=1):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        index = block_no
        if lines[0].strip().isdigit():
            index = int(lines[0].strip())
            lines = lines[1:]
        if not lines:
            continue
        match = SRT_TIME.match(lines[0].strip())
        if not match:
            continue
        cues.append(SubtitleCue(
            index=index,
            start=match.group("start").replace(".", ","),
            end=match.group("end").replace(".", ","),
            text="\n".join(lines[1:]).strip(),
        ))
    return cues


def render_srt(cues: List[SubtitleCue]) -> str:
    blocks = []
    for output_index, cue in enumerate(cues, start=1):
        text = (cue.text or "").strip()
        blocks.append(f"{output_index}\n{cue.start} --> {cue.end}\n{text}")
    return "\n\n".join(blocks) + "\n"


def parse_ass(content: str) -> Tuple[List[str], List[SubtitleCue]]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    in_events = False
    format_fields: List[str] = []
    text_index = -1
    cue_index = 1

    for line_no, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "[events]":
            in_events = True
            continue
        if in_events and stripped.startswith("[") and stripped.endswith("]"):
            in_events = False
        if not in_events:
            continue
        if stripped.lower().startswith("format:"):
            format_fields = [part.strip().lower() for part in stripped.split(":", 1)[1].split(",")]
            try:
                text_index = format_fields.index("text")
            except ValueError:
                text_index = -1
            continue
        if not stripped.lower().startswith("dialogue:") or text_index < 0:
            continue
        raw = line.split(":", 1)[1].lstrip()
        fields = raw.split(",", len(format_fields) - 1)
        if len(fields) <= text_index:
            continue
        start = fields[format_fields.index("start")] if "start" in format_fields else ""
        end = fields[format_fields.index("end")] if "end" in format_fields else ""
        cues.append(SubtitleCue(
            index=cue_index,
            start=start,
            end=end,
            text=fields[text_index].replace("\\N", "\n"),
            line_index=line_no,
            ass_fields=fields,
            ass_text_index=text_index,
        ))
        cue_index += 1
    return lines, cues


def render_ass(lines: List[str], cues: List[SubtitleCue]) -> str:
    for cue in cues:
        if cue.line_index is None or cue.ass_fields is None or cue.ass_text_index is None:
            continue
        fields = list(cue.ass_fields)
        fields[cue.ass_text_index] = (cue.text or "").replace("\n", "\\N")
        lines[cue.line_index] = "Dialogue: " + ",".join(fields)
    return "\n".join(lines)
