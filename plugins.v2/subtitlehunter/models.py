# _*_ coding: utf-8 _*_
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SubtitleTrack:
    source: str
    path: Optional[Path]
    video_path: Optional[Path]
    stream_index: Optional[int]
    codec: str
    language: str
    title: str
    forced: bool
    default: bool
    text_based: bool
    extension: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": str(self.path) if self.path else "",
            "video_path": str(self.video_path) if self.video_path else "",
            "stream_index": self.stream_index,
            "codec": self.codec,
            "language": self.language,
            "title": self.title,
            "forced": self.forced,
            "default": self.default,
            "text_based": self.text_based,
            "extension": self.extension,
        }


@dataclass
class SubtitleCue:
    index: int
    start: str
    end: str
    text: str
    line_index: Optional[int] = None
    ass_fields: Optional[List[str]] = None
    ass_text_index: Optional[int] = None
