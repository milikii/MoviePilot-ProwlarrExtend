# _*_ coding: utf-8 _*_
"""Pure mapping helpers for Prowlarr → MoviePilot TorrentInfo fields."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


def normalize_pubdate(value: Any) -> Any:
    """Convert Prowlarr ISO timestamps to MoviePilot's local naive format."""
    if not isinstance(value, str) or not value.strip():
        return value
    raw_value = value.strip()
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw_value


def normalize_imdb_id(value: Any) -> Optional[str]:
    if value in (None, "", 0, "0"):
        return None
    raw_value = str(value).strip()
    if raw_value.lower().startswith("tt"):
        return raw_value
    if raw_value.isdigit():
        return f"tt{int(raw_value):07d}"
    return raw_value


def volume_factors(entry: Dict[str, Any]) -> Tuple[float, float]:
    """Derive download/upload volume factors from Prowlarr fields and flags."""

    def factor(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    download_factor = factor(entry.get("downloadVolumeFactor"))
    upload_factor = factor(entry.get("uploadVolumeFactor"))
    flags = {
        re.sub(r"[^a-z0-9]", "", str(flag).lower())
        for flag in (entry.get("indexerFlags") or [])
    }

    if download_factor is None:
        if "freeleech" in flags or "neutralleech" in flags:
            download_factor = 0.0
        elif "freeleech75" in flags:
            download_factor = 0.25
        elif "halfleech" in flags:
            download_factor = 0.5
        elif "freeleech25" in flags:
            download_factor = 0.75
        else:
            download_factor = 1.0
    if upload_factor is None:
        if "neutralleech" in flags:
            upload_factor = 0.0
        else:
            upload_factor = 2.0 if "doubleupload" in flags else 1.0
    return download_factor, upload_factor


def infer_category(categories: list) -> str:
    for cat in categories or []:
        if not isinstance(cat, dict):
            continue
        cat_id = cat.get("id", 0)
        try:
            cat_id = int(cat_id)
        except (TypeError, ValueError):
            continue
        if 2000 <= cat_id < 3000:
            return "电影"
        if 5000 <= cat_id < 6000:
            return "电视剧"
    return ""
