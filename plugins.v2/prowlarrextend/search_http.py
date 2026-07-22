# _*_ coding: utf-8 _*_
"""Pure helpers for Prowlarr search HTTP outcomes and timeouts."""
from __future__ import annotations

import json
from typing import Any, Optional, Tuple

DEFAULT_SEARCH_TIMEOUT = 30
MIN_SEARCH_TIMEOUT = 5
MAX_SEARCH_TIMEOUT = 120


def normalize_search_timeout(value: Any, default: int = DEFAULT_SEARCH_TIMEOUT) -> int:
    """Clamp search timeout (seconds) to a safe range."""
    try:
        if value is None or value == "":
            timeout = int(default)
        else:
            timeout = int(float(value))
    except (TypeError, ValueError):
        timeout = int(default)
    return max(MIN_SEARCH_TIMEOUT, min(MAX_SEARCH_TIMEOUT, timeout))


def extract_prowlarr_error(payload: Any, text: str = "") -> str:
    """Best-effort human message from Prowlarr error body."""
    if isinstance(payload, dict):
        for key in ("message", "description", "error", "title"):
            value = payload.get(key)
            if value:
                return str(value).strip()
        # Fall through to compact json
        try:
            return json.dumps(payload, ensure_ascii=False)[:300]
        except Exception:
            pass
    if isinstance(payload, list) and payload:
        return f"unexpected list body ({len(payload)} items)"
    raw = (text or "").strip()
    if raw:
        return raw[:300]
    return ""


def parse_response_payload(response: Any) -> Tuple[Optional[Any], str]:
    """
    Parse response body as JSON when possible.

    Returns (payload, raw_text). payload is None when JSON parse fails.
    """
    raw = ""
    try:
        raw = getattr(response, "text", None) or ""
    except Exception:
        raw = ""
    try:
        return response.json(), raw
    except Exception:
        pass
    if raw:
        try:
            return json.loads(raw), raw
        except Exception:
            return None, raw
    return None, raw


def classify_search_http(
    response: Any,
    *,
    elapsed_ms: int,
    timeout: int,
) -> Tuple[str, str]:
    """
    Classify a search HTTP outcome.

    Returns (kind, detail) where kind is one of:
      no_response | http_error | bad_json | empty | ok
    """
    if response is None:
        return (
            "no_response",
            f"请求失败或超时（timeout={timeout}s, {elapsed_ms}ms）",
        )

    status = getattr(response, "status_code", None)
    payload, raw = parse_response_payload(response)

    if status is not None and int(status) >= 400:
        err = extract_prowlarr_error(payload, raw) or f"HTTP {status}"
        return "http_error", f"HTTP {status}：{err}（{elapsed_ms}ms）"

    if not isinstance(payload, list):
        err = extract_prowlarr_error(payload, raw) or "返回数据不是 JSON 列表"
        return "bad_json", f"{err}（{elapsed_ms}ms）"

    if not payload:
        return "empty", f"无结果（{elapsed_ms}ms）"

    return "ok", f"{len(payload)} 条（{elapsed_ms}ms）"
