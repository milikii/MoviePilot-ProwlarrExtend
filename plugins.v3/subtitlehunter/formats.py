# _*_ coding: utf-8 _*_
"""Subtitle parse/render and text helpers (pure, no plugin state)."""
from __future__ import annotations

import codecs
import re
from typing import List, Tuple

from .constants import SRT_TIME
from .models import SubtitleCue


def plain_subtitle_text(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", text or "")
    cleaned = re.sub(r"\{[^}]*}", "", cleaned)
    cleaned = cleaned.replace("\\N", "\n")
    return cleaned.strip()


class TagError(ValueError):
    pass


TAG_TOKEN = re.compile(r"\[\[SH_TAG_\d+\]\]")
TAG_BLOCK = re.compile(r"\{[^{}]*\}|<[^<>]*>")


def tag_layout(text: str):
    """Support balanced SRT styles and simple ASS overrides; reject ambiguous syntax."""
    text = text or ""
    if "[[SH_TAG_" in text:
        raise TagError("标签占位符冲突")
    matches = list(TAG_BLOCK.finditer(text))
    remaining = TAG_BLOCK.sub("", text)
    if any(char in remaining for char in "{}<>"):
        raise TagError("不支持的复杂标签/括号场景")
    stack = []
    for match in matches:
        tag = match.group()
        if tag.startswith("{"):
            # Drawing, karaoke, transforms, clipping and move have semantic positions.
            if not re.fullmatch(r"\{(?:\\(?:an[1-9]|[ibu][01]|s[01]|r[^\\{}]*|"
                                r"pos\([-+\d.]+,[-+\d.]+\)|"
                                r"(?:[1-4]?[ca]|fn|fs|bord|shad)[^\\{}()]*))+\}", tag):
                raise TagError(f"不支持的复杂 ASS 标签：{tag}")
        else:
            parsed = re.fullmatch(r"<(/?)(i|b|u|s|font)(\s+[^<>]*)?>", tag, re.I)
            if not parsed:
                raise TagError(f"不支持的 SRT 标签：{tag}")
            closing, name, attrs = parsed.groups()
            name = name.lower()
            if attrs and (closing or name != "font" or not re.fullmatch(
                    r'''(?:\s+(?:color|face|size)\s*=\s*(?:"[^"<>]*"|'[^'<>]*'|[^\s<>]+))+\s*''', attrs, re.I)):
                raise TagError(f"不支持的标签属性：{tag}")
            if closing:
                if not stack or stack.pop() != name:
                    raise TagError("SRT 标签嵌套不匹配")
            else:
                stack.append(name)
    if stack:
        raise TagError("SRT 标签未闭合")
    return matches


def mask_tags(text: str) -> str:
    matches = tag_layout(text)
    output, offset = [], 0
    for number, match in enumerate(matches):
        output.extend((text[offset:match.start()], f"[[SH_TAG_{number:03}]]"))
        offset = match.end()
    output.append(text[offset:])
    return "".join(output)


def restore_tags(source: str, translated: str) -> str:
    matches = tag_layout(source)
    tags = [match.group() for match in matches]
    expected = [f"[[SH_TAG_{number:03}]]" for number in range(len(tags))]
    tokens = TAG_TOKEN.findall(translated)
    if tokens:
        if tokens != expected or TAG_BLOCK.search(translated):
            raise TagError("标签占位符数量/顺序不匹配")
        for token, tag in zip(expected, tags):
            translated = translated.replace(token, tag)
    else:
        actual = [match.group() for match in tag_layout(translated)]
        if actual != tags:
            if actual or not tags:
                raise TagError("译文标签与原文不匹配")
            # Position recovery is safe only for tags outside the whole visible text.
            prefix, suffix = [], []
            for match in matches:
                before = TAG_BLOCK.sub("", source[:match.start()]).strip()
                after = TAG_BLOCK.sub("", source[match.end():]).strip()
                if not before:
                    prefix.append(match.group())
                elif not after:
                    suffix.append(match.group())
                else:
                    raise TagError("中途标签丢失，无法按位置安全恢复")
            translated = "".join(prefix) + translated + "".join(suffix)
    if [match.group() for match in tag_layout(translated)] != tags:
        raise TagError("标签回填校验失败")
    return translated


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


class ParsedCues(list):
    """List-compatible parser result; malformed blocks remain observable."""
    def __init__(self, cues=(), diagnostics=()):
        super().__init__(cues)
        self.diagnostics = list(diagnostics)
        self.bad_blocks = len(self.diagnostics)


def decode_subtitle(data: bytes) -> Tuple[str, str]:
    # A recognized BOM is authoritative: never reinterpret a broken BOM file.
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"),
                          (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"),
                          (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data.decode(encoding, errors="strict"), encoding
    failures = []
    for encoding in ("utf-8", "gbk", "big5"):
        try:
            content = data.decode(encoding, errors="strict")
            if "\x00" in content:
                raise ValueError("无 BOM 的 NUL 字节，编码不明确")
            return content, encoding
        except (UnicodeError, ValueError) as error:
            failures.append(f"{encoding}: {error}")
    raise ValueError("字幕严格解码失败：" + "; ".join(failures))


def normalize_srt_time(value: str) -> str:
    parts = re.fullmatch(r"(\d+):(\d+):(\d+)(?:[,.](\d{1,3}))?", value)
    if not parts:
        raise ValueError("无法解析时间")
    hours, minutes, seconds = map(int, parts.groups()[:3])
    if minutes >= 60 or seconds >= 60:
        raise ValueError("分钟或秒超出范围")
    milliseconds = (parts.group(4) or "").ljust(3, "0")
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds}"


def parse_srt(content: str) -> ParsedCues:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    cues = ParsedCues()
    if not normalized:
        return cues
    for block_no, block in enumerate(re.split(r"(?:\n[ \t]*){2,}", normalized), start=1):
        lines = block.split("\n")
        index = block_no
        if lines[0].strip().isdigit():
            index = int(lines.pop(0).strip())
        try:
            match = SRT_TIME.fullmatch(lines[0].strip()) if lines else None
            if not match:
                raise ValueError("缺少或不支持的时轴")
            start = normalize_srt_time(match.group("start"))
            end = normalize_srt_time(match.group("end"))
            if tuple(map(int, re.split(r"[:,]", end))) <= tuple(map(int, re.split(r"[:,]", start))):
                raise ValueError("结束时间不晚于开始时间")
            tail = match.group("tail")
            if tail and not re.fullmatch(r"(?:\s+[XY][12]:\d+)+", tail):
                raise ValueError("不支持的时轴附加字段")
            cues.append(SubtitleCue(index, start, end, "\n".join(lines[1:]).strip(),
                                    cue_id=block_no, time_tail=tail))
        except ValueError as error:
            cues.diagnostics.append({"block": block_no, "snippet": block[:160], "reason": str(error)})
    cues.bad_blocks = len(cues.diagnostics)
    return cues


def render_srt(cues: List[SubtitleCue]) -> str:
    blocks = []
    for output_index, cue in enumerate(cues, start=1):
        text = (cue.text or "").strip()
        blocks.append(f"{output_index}\n{cue.start} --> {cue.end}{cue.time_tail}\n{text}")
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
            cue_id=cue_index,
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
    lines = list(lines)
    for cue in cues:
        if cue.line_index is None or cue.ass_fields is None or cue.ass_text_index is None:
            continue
        fields = list(cue.ass_fields)
        fields[cue.ass_text_index] = (cue.text or "").replace("\n", "\\N")
        lines[cue.line_index] = "Dialogue: " + ",".join(fields)
    return "\n".join(lines)
