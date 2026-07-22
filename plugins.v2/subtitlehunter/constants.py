# _*_ coding: utf-8 _*_
import re

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".flv",
    ".ts", ".m2ts", ".mts", ".webm",
}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
TRANSLATABLE_EXTS = {".srt", ".ass", ".ssa"}
TEXT_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text",
    "realtext", "microdvd", "mpl2", "sami",
}
IMAGE_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub",
}
CHI_PATTERNS = re.compile(
    r"((^|[._\-\s\[\]()])"
    r"(chi|chs|cht|chinese|zh([_-]?(cn|hans|hant|tw|hk))?|zho|cmn)"
    r"(?=$|[._\-\s\[\]()]))|中文|简体|繁体|简中|繁中|双语|中英",
    re.IGNORECASE,
)
ENG_PATTERNS = re.compile(
    r"((^|[._\-\s\[\]()])(eng|en|english)(?=$|[._\-\s\[\]()]))|英文|英语",
    re.IGNORECASE,
)
FORCED_PATTERNS = re.compile(
    r"(^|[._\-\s\[\]()])(forced|foreign|only|signs?)(?=$|[._\-\s\[\]()])|强制|特效",
    re.IGNORECASE,
)
SRT_TIME = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})(?P<tail>.*)"
)
CACHE_MAX_FILES = 500
CACHE_MAX_AGE_DAYS = 30
CHINESE_COVERAGE_THRESHOLD = 0.5
