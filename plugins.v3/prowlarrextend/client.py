# _*_ coding: utf-8 _*_
"""Prowlarr HTTP calls with explicit errors and a shared wall-clock budget."""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger


DEFAULT_SEARCH_TIMEOUT = 30
MIN_SEARCH_TIMEOUT = 5
MAX_SEARCH_TIMEOUT = 120


def normalize_search_timeout(value: Any, default: int = DEFAULT_SEARCH_TIMEOUT) -> int:
    try:
        timeout = int(float(value)) if value not in (None, "") else int(default)
    except (TypeError, ValueError, OverflowError):
        timeout = int(default)
    return max(MIN_SEARCH_TIMEOUT, min(MAX_SEARCH_TIMEOUT, timeout))


class ProwlarrError(Exception):
    """A diagnostic outcome; HTTP failures never become an empty search."""

    LABELS = {
        "authentication": "认证错误",
        "not_found": "端点不存在",
        "rate_limit": "请求限流",
        "server_error": "Prowlarr 服务端错误",
        "http_error": "HTTP 请求错误",
        "network": "网络异常",
        "timeout": "总超时预算耗尽",
        "bad_response": "响应格式异常",
        "configuration": "配置错误",
    }

    def __init__(self, kind: str, detail: str, status_code: Optional[int] = None):
        self.kind = kind
        self.detail = detail
        self.status_code = status_code
        label = self.LABELS.get(kind, kind)
        status = f" HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"{label}{status}：{detail}")


class ProwlarrClient:
    def __init__(
        self,
        host: str,
        api_key: str,
        *,
        user_agent: str = "ProwlarrExtend",
        proxies: Optional[Dict[str, str]] = None,
        timeout: int = DEFAULT_SEARCH_TIMEOUT,
    ):
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.proxies = dict(proxies or {})
        self.timeout = normalize_search_timeout(timeout)
        self.headers = {
            "X-Api-Key": api_key,
            "User-Agent": user_agent,
            "Accept": "application/json",
        }

    @staticmethod
    def _status_kind(status: int) -> str:
        if status in (401, 403):
            return "authentication"
        if status == 404:
            return "not_found"
        if status == 429:
            return "rate_limit"
        if 500 <= status < 600:
            return "server_error"
        return "http_error"

    def _safe_detail(self, value: Any) -> str:
        text = str(value)
        if self.api_key:
            text = text.replace(self.api_key, "[redacted]")
        return text[:300]

    def _error_detail(self, response: requests.Response) -> str:
        # Body decoding is best effort AFTER the HTTP status has been classified.
        # Even an unreadable error body must retain its 401/404/429/5xx outcome.
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("message", "description", "error", "title"):
                    if payload.get(key):
                        return self._safe_detail(payload[key])
            return self._safe_detail(json.dumps(payload, ensure_ascii=False))
        except (ValueError, requests.RequestException):
            try:
                return self._safe_detail(response.text.strip()) or "错误响应体为空"
            except requests.RequestException:
                return "无法读取错误响应体"

    def _request_json(
        self,
        path: str,
        *,
        expected_type: type,
        context: str,
        params: Optional[List[Tuple[str, Any]]] = None,
        deadline: Optional[float] = None,
    ) -> Any:
        started = time.monotonic()
        deadline = deadline if deadline is not None else started + self.timeout
        remaining = deadline - started
        url = f"{self.host}/api/v1/{path.lstrip('/')}"
        outcomes: queue.Queue = queue.Queue(maxsize=1)
        cancelled = threading.Event()
        # Record the status immediately, so a slow/unreadable 4xx/5xx body cannot
        # turn a known HTTP failure into a generic timeout or no-response message.
        status_seen: List[int] = []

        def request() -> None:
            payload = None
            error = None
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    session.proxies.update(self.proxies)
                    with session.get(
                        url,
                        headers=self.headers,
                        params=params,
                        timeout=(min(5.0, remaining), remaining),
                        allow_redirects=False,
                        stream=True,
                    ) as response:
                        status = response.status_code
                        status_seen.append(status)
                        if not 200 <= status < 300:
                            kind = self._status_kind(status)
                            detail = self._error_detail(response)
                            if status == 429 and response.headers.get("Retry-After"):
                                detail += f"；Retry-After={response.headers['Retry-After']}"
                            raise ProwlarrError(kind, detail, status)
                        if cancelled.is_set():
                            return
                        payload = response.json()
                        if not isinstance(payload, expected_type):
                            raise ProwlarrError(
                                "bad_response", f"预期 JSON {expected_type.__name__}", status
                            )
                        if isinstance(payload, list) and any(
                            not isinstance(item, dict) for item in payload
                        ):
                            raise ProwlarrError("bad_response", "列表包含非对象条目", status)
            except ProwlarrError as exc:
                error = exc
            except requests.Timeout as exc:
                error = ProwlarrError("timeout", self._safe_detail(exc))
            except requests.RequestException as exc:
                error = ProwlarrError("network", self._safe_detail(exc))
            except ValueError as exc:
                error = ProwlarrError("bad_response", self._safe_detail(exc))
            except Exception as exc:
                error = ProwlarrError("network", self._safe_detail(exc))
            if not cancelled.is_set():
                outcomes.put((payload, error))

        try:
            if not self.host or not self.api_key:
                raise ProwlarrError("configuration", "未配置 Prowlarr 地址或 API Key")
            if remaining <= 0:
                raise ProwlarrError("timeout", "请求开始前已达到截止时间")
            # requests' read timeout alone is not a wall-clock limit. Waiting for
            # this isolated, read-only worker also bounds slow bodies and JSON
            # decoding; abandoned workers cannot publish plugin state.
            threading.Thread(
                target=request, daemon=True, name="ProwlarrExtendHTTP"
            ).start()
            try:
                payload, error = outcomes.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                cancelled.set()
                status = status_seen[0] if status_seen else None
                if status is not None and not 200 <= status < 300:
                    raise ProwlarrError(
                        self._status_kind(status), "读取错误响应体时达到总截止时间", status
                    ) from None
                raise ProwlarrError("timeout", "HTTP 请求达到总截止时间") from None
            if error is not None:
                raise error
            return payload
        except ProwlarrError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.error(
                f"【ProwlarrExtend】{context} GET {url}：{exc} "
                f"（剩余预算={max(0.0, remaining):.2f}s，耗时={elapsed_ms}ms）"
            )
            raise

    def get_indexers(self, *, deadline: Optional[float] = None) -> List[Dict[str, Any]]:
        return self._request_json(
            "indexer", expected_type=list, context="读取索引器目录", deadline=deadline
        )

    def get_system_status(self, *, deadline: Optional[float] = None) -> Dict[str, Any]:
        return self._request_json(
            "system/status", expected_type=dict, context="只读连接探测", deadline=deadline
        )

    def probe_connection(self, *, deadline: Optional[float] = None) -> Dict[str, Any]:
        """Read system status and indexer configuration, without caching either."""
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout
        status = self.get_system_status(deadline=deadline)
        # A failed indexer probe must propagate even when system/status succeeds.
        indexers = self.get_indexers(deadline=deadline)
        return {"status": status, "indexers": indexers}

    def search(
        self,
        indexer_ids: List[str],
        keyword: str,
        *,
        categories: Optional[List[int]] = None,
        page: int = 0,
        page_size: int = 100,
        deadline: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        if not indexer_ids:
            return []
        params = [("query", keyword)]
        params.extend(("indexerIds", indexer_id) for indexer_id in indexer_ids)
        params.extend([
            ("type", "search"),
            ("limit", page_size),
            ("offset", max(0, page) * page_size),
        ])
        params.extend(("categories", category) for category in (categories or []))
        return self._request_json(
            "search",
            expected_type=list,
            context=f"聚合检索 indexerIds={indexer_ids} keyword={keyword!r} page={page}",
            params=params,
            deadline=deadline,
        )
