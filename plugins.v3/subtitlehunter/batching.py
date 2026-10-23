# _*_ coding: utf-8 _*_
"""Pure cue batching helpers for AI translation."""
from __future__ import annotations

from typing import Callable, List, Sequence, TypeVar

T = TypeVar("T")

BoundaryFn = Callable[[str], bool]
TextFn = Callable[[T], str]


def chunk_cues(
    cues: Sequence[T],
    batch_size: int,
    batch_chars: int,
    *,
    text_fn: TextFn = lambda cue: getattr(cue, "text", ""),
    is_sentence_boundary: BoundaryFn,
    lookahead: int = 5,
) -> List[List[T]]:
    """Split cues into batches preferring sentence boundaries near soft limits."""
    if not cues:
        return []
    batch_size = max(1, int(batch_size))
    batch_chars = max(1, int(batch_chars))
    chunks: List[List[T]] = []
    total = len(cues)
    start = 0
    while start < total:
        end = start
        chars = 0
        soft_count = max(1, int(batch_size * 0.9))
        soft_chars = max(1, int(batch_chars * 0.9))
        while end < total:
            text_len = len(text_fn(cues[end]))
            if end > start and (end - start >= batch_size or chars + text_len > batch_chars):
                break
            chars += text_len
            end += 1
            if end >= total or end - start >= soft_count or chars >= soft_chars:
                break

        if end >= total:
            chunks.append(list(cues[start:end]))
            break

        cut = end if is_sentence_boundary(text_fn(cues[end - 1])) else 0
        probe_end = end
        probe_chars = chars
        while not cut and probe_end < total and probe_end - end < lookahead:
            text_len = len(text_fn(cues[probe_end]))
            if probe_end - start >= batch_size or probe_chars + text_len > batch_chars:
                break
            probe_chars += text_len
            probe_end += 1
            if is_sentence_boundary(text_fn(cues[probe_end - 1])):
                cut = probe_end
                break

        if not cut:
            hard_end = end
            hard_chars = chars
            while hard_end < total:
                text_len = len(text_fn(cues[hard_end]))
                if hard_end - start >= batch_size or hard_chars + text_len > batch_chars:
                    break
                hard_chars += text_len
                hard_end += 1
            cut = hard_end

        chunks.append(list(cues[start:cut]))
        start = cut
    return chunks
