# _*_ coding: utf-8 _*_
"""MoviePilot v3 plugin resource source backed by Prowlarr."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.domain.context import TorrentInfo
from app.schemas.types import MediaSource

from .client import DEFAULT_SEARCH_TIMEOUT, ProwlarrClient, ProwlarrError, normalize_search_timeout
from .mapping import infer_category, normalize_imdb_id, normalize_pubdate, volume_factors
from . import selection


class ProwlarrExtend(_PluginBase):
    plugin_name = "ProwlarrExtend"
    plugin_desc = "扩展检索以支持Prowlarr站点资源"
    plugin_icon = "Prowlarr.png"
    plugin_version = "3.0.1"
    plugin_author = "milikii"
    author_url = "https://github.com/milikii"
    plugin_label = ""
    plugin_config_prefix = "prowlarr_extend_"
    plugin_order = 16
    auth_level = 1

    _DEFAULT_CRON = "0 0 * * *"
    _SEARCH_PAGE_SIZE = 100
    # One reference publishes selected indexers, the full catalog and its validity
    # together. Readers never observe half of a successful directory refresh.
    _snapshot: Tuple[tuple, tuple, bool] = ((), (), False)
    _enabled = False

    def init_plugin(self, config: Optional[Mapping[str, Any]] = None):
        self.stop_service()
        self._lock = getattr(self, "_lock", None) or threading.RLock()
        saved = self.get_config() or {}
        values = {**self._defaults(), **saved, **dict(config or {})}
        host = str(values.get("host") or "").strip().rstrip("/")
        if host and "://" not in host:
            host = "http://" + host
        values["host"] = host
        values["api_key"] = str(values.get("api_key") or "").strip()
        values["search_timeout"] = normalize_search_timeout(values.get("search_timeout"))
        values["selected_indexers"] = selection.normalize_selected_indexers(
            values.get("selected_indexers"), self.plugin_name
        )
        values["indexer_catalog"] = selection.normalize_indexer_catalog(
            values.get("indexer_catalog")
        ) or selection.normalize_indexer_catalog(saved.get("indexer_catalog"))
        values["cron"] = values.get("cron") or self._DEFAULT_CRON
        if values["cron"] == "0 0 */24 * *":
            values["cron"] = self._DEFAULT_CRON

        with self._lock:
            self._config = values
            self._enabled = bool(values["enabled"])
            self._generation = object()
            self._stop_event = threading.Event()
            self._snapshot = ((), tuple(values["indexer_catalog"]), False)
            self._client = ProwlarrClient(
                host,
                values["api_key"],
                user_agent=settings.USER_AGENT,
                proxies=settings.PROXY if values["proxy"] else None,
                timeout=values["search_timeout"],
            )
            generation = self._generation
            client = self._client
            selected = list(values["selected_indexers"])
            run_once = bool(values["onlyonce"])
            values["onlyonce"] = False
            if run_once or values["cron"] != saved.get("cron", self._DEFAULT_CRON):
                self.update_config(dict(values))

        if host and values["api_key"]:
            if run_once:
                self._refresh_snapshot(client, generation, selected)
            else:
                self._init_thread = threading.Thread(
                    target=self._refresh_snapshot,
                    args=(client, generation, selected),
                    daemon=True,
                    name="ProwlarrExtendInitSync",
                )
                self._init_thread.start()

    @staticmethod
    def _defaults() -> Dict[str, Any]:
        return {
            "host": "", "api_key": "", "cron": "0 0 * * *",
            "enabled": False, "proxy": False, "onlyonce": False,
            "search_timeout": DEFAULT_SEARCH_TIMEOUT,
            "selected_indexers": [], "indexer_catalog": [],
        }

    @staticmethod
    def _indexer_id(indexer: Dict[str, Any]) -> str:
        value = str(indexer.get("id") or "").strip()
        return value if value.isdigit() and int(value) > 0 else ""

    @classmethod
    def _eligible_indexers(cls, raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        eligible = []
        seen = set()
        for item in raw:
            indexer_id = cls._indexer_id(item)
            name = str(item.get("name") or item.get("definitionName") or "").strip()
            if not indexer_id or not name or indexer_id in seen:
                continue
            if item.get("enable") is False or item.get("supportsSearch") is False:
                continue
            protocol = item.get("protocol")
            if isinstance(protocol, str) and protocol.lower() != "torrent":
                continue
            seen.add(indexer_id)
            eligible.append({
                "id": indexer_id, "name": name,
                "privacy": str(item.get("privacy") or "unknown").lower(),
            })
        return eligible

    def _refresh_snapshot(
        self,
        client: ProwlarrClient,
        generation: object,
        selected: List[str],
        *,
        deadline: Optional[float] = None,
        persist: bool = True,
    ) -> bool:
        try:
            eligible = self._eligible_indexers(client.get_indexers(deadline=deadline))
        except ProwlarrError:
            logger.warning(f"【{self.plugin_name}】索引器刷新失败，保留上次成功快照")
            return False
        catalog = selection.normalize_indexer_catalog(eligible)
        chosen = selection.apply_indexer_selection(eligible, selected, self._indexer_id)
        snapshot = (tuple(chosen), tuple(catalog), True)
        with self._lock:
            if generation is not self._generation or self._stop_event.is_set():
                return False
            self._snapshot = snapshot
            self._config["indexer_catalog"] = catalog
            if persist:
                self.update_config(dict(self._config))
        logger.info(
            f"【{self.plugin_name}】索引器目录更新：可用 {len(catalog)} 个，"
            f"选择 {len(chosen)} 个"
        )
        # A valid empty directory is a successful refresh, unlike an HTTP failure.
        return True

    def sync_indexers(self) -> bool:
        if not hasattr(self, "_client"):
            return False
        with self._lock:
            client, generation = self._client, self._generation
            selected = list(self._config["selected_indexers"])
            if self._stop_event.is_set():
                return False
        return self._refresh_snapshot(client, generation, selected)

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        cron = self._config["cron"]
        try:
            trigger = CronTrigger.from_crontab(cron)
        except (TypeError, ValueError) as exc:
            logger.error(f"【{self.plugin_name}】更新周期 {cron!r} 无效：{exc}")
            return []
        return [{
            "id": "ProwlarrExtendRefresh", "name": "Prowlarr 索引刷新",
            "trigger": trigger, "func": self.sync_indexers, "kwargs": {},
        }]

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_module(self) -> Dict[str, Any]:
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    def stop_service(self):
        stop = getattr(self, "_stop_event", None)
        if stop is not None:
            stop.set()
        worker = getattr(self, "_init_thread", None)
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1)

    @staticmethod
    def get_cat(mtype: Any = None) -> List[int]:
        name = getattr(mtype, "name", "")
        value = getattr(mtype, "value", mtype)
        if name == "MOVIE" or value in ("电影", "Movie", "movie", "MOVIE"):
            return [2000]
        if name == "TV" or value in ("电视剧", "TV", "tv"):
            return [5000]
        return [2000, 5000]

    def search_torrents(
        self, site: dict, keyword: str, mtype: Any = None, page: Optional[int] = 0
    ) -> List[TorrentInfo]:
        # The v3 resource-source dispatcher supplies {}. Ordinary site searches
        # belong to the host's separate site-search path.
        if site or not keyword or not self.get_state() or not hasattr(self, "_client"):
            return []
        started = time.monotonic()
        with self._lock:
            client, generation = self._client, self._generation
            selected = list(self._config["selected_indexers"])
            snapshot = self._snapshot
            proxy = bool(self._config["proxy"])
            if self._stop_event.is_set():
                return []
        # The initial catalog fetch, when needed, and the aggregate search share
        # this deadline. Adding indexers never multiplies the total waiting time.
        deadline = started + client.timeout
        if not snapshot[2]:
            if not self._refresh_snapshot(
                client, generation, selected, deadline=deadline, persist=False
            ):
                return []
            with self._lock:
                if generation is not self._generation or self._stop_event.is_set():
                    return []
                snapshot = self._snapshot
        indexers = snapshot[0]
        if not indexers:
            logger.info(f"【{self.plugin_name}】当前选择没有可用 Torrent 索引器")
            return []
        names = {item["id"]: item["name"] for item in indexers}
        try:
            page_number = max(0, int(page or 0))
        except (TypeError, ValueError, OverflowError):
            page_number = 0
        try:
            entries = client.search(
                list(names), keyword, categories=self.get_cat(mtype), page=page_number,
                page_size=self._SEARCH_PAGE_SIZE, deadline=deadline,
            )
        except ProwlarrError:
            return []

        results: List[TorrentInfo] = []
        seen = set()
        for entry in entries:
            if time.monotonic() >= deadline:
                logger.warning(f"【{self.plugin_name}】结果映射达到总截止时间，保留已映射结果")
                break
            indexer_id = str(entry.get("indexerId") or "").strip()
            indexer_name = str(entry.get("indexer") or entry.get("indexerName") or "").strip()
            if not indexer_id:
                matches = [key for key, name in names.items() if name == indexer_name]
                if len(matches) == 1:
                    indexer_id = matches[0]
                elif len(names) == 1:
                    indexer_id = next(iter(names))
            if indexer_id not in names:
                logger.warning(
                    f"【{self.plugin_name}】跳过不在选择范围或身份不明确的结果："
                    f"indexerId={indexer_id!r} indexer={indexer_name!r}"
                )
                continue
            enclosure = entry.get("downloadUrl") or entry.get("magnetUrl")
            if not entry.get("title") or not enclosure:
                logger.warning(f"【{self.plugin_name}】索引器 {names[indexer_id]} 的结果缺少标题或下载链接")
                continue
            # Deduplicate within an indexer. The same torrent from another
            # indexer keeps its own identity, URL and volume factors.
            identity = (indexer_id, str(entry.get("guid") or enclosure))
            if identity in seen:
                continue
            try:
                categories = entry.get("categories") or []
                if not isinstance(categories, list):
                    categories = []
                labels = [
                    category["name"] for category in categories
                    if isinstance(category, dict) and category.get("name")
                ]
                download_factor, upload_factor = volume_factors(entry)
                imdb_id = normalize_imdb_id(entry.get("imdbId"))
                torrent = TorrentInfo(
                    site_name=indexer_name or names[indexer_id],
                    site_cookie=None,
                    site_ua=settings.USER_AGENT,
                    site_proxy=proxy,
                    site_order=0,
                    site_downloader=None,
                    title=entry.get("title"),
                    enclosure=enclosure,
                    description=entry.get("sortTitle"),
                    size=entry.get("size"),
                    seeders=entry.get("seeders"),
                    peers=entry.get("leechers"),
                    grabs=entry.get("grabs"),
                    pubdate=normalize_pubdate(entry.get("publishDate")),
                    page_url=entry.get("infoUrl") or entry.get("guid"),
                    media_source=MediaSource.IMDb if imdb_id else None,
                    media_id=imdb_id,
                    labels=labels,
                    category=infer_category(categories),
                    downloadvolumefactor=download_factor,
                    uploadvolumefactor=upload_factor,
                )
            except (TypeError, ValueError) as exc:
                logger.warning(
                    f"【{self.plugin_name}】索引器 {names[indexer_id]} 结果映射失败：{exc}"
                )
                continue
            seen.add(identity)
            results.append(torrent)
        with self._lock:
            if generation is not self._generation or self._stop_event.is_set():
                return []
        logger.info(
            f"【{self.plugin_name}】聚合检索完成：{len(indexers)} 个索引器，"
            f"{len(results)} 条，关键词={keyword!r}，"
            f"耗时={int((time.monotonic() - started) * 1000)}ms"
        )
        return results

    async def async_search_torrents(
        self, site: dict, keyword: str, mtype: Any = None, page: Optional[int] = 0
    ) -> List[TorrentInfo]:
        return await run_in_threadpool(
            self.search_torrents, site=site, keyword=keyword, mtype=mtype, page=page
        )

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/test", "endpoint": self.test_connection, "methods": ["GET"],
             "auth": "apikey", "summary": "测试 Prowlarr 连接"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "apikey", "summary": "获取 ProwlarrExtend 同步状态"},
        ]

    def test_connection(self) -> Dict[str, Any]:
        if not hasattr(self, "_client"):
            return {"success": False, "message": "插件尚未初始化", "host": ""}
        with self._lock:
            client = self._client
            selected = list(self._config["selected_indexers"])
        try:
            probe = client.probe_connection()
            eligible = self._eligible_indexers(probe["indexers"])
            chosen = selection.apply_indexer_selection(eligible, selected, self._indexer_id)
        except ProwlarrError as exc:
            return {
                "success": False, "message": f"连接失败：{exc}",
                "host": client.host, "error": exc.kind, "status_code": exc.status_code,
            }
        return {
            "success": True,
            "message": f"连接成功，可用 Torrent 索引器 {len(eligible)} 个，当前选择 {len(chosen)} 个",
            "host": client.host,
            "version": str(probe["status"].get("version") or probe["status"].get("appName") or ""),
            "indexers": len(chosen), "available": len(eligible),
            "selected": selected, "authoritative": True,
        }

    def api_status(self) -> Dict[str, Any]:
        if not hasattr(self, "_config"):
            return {"enabled": False, "authoritative": False, "indexers": 0}
        with self._lock:
            snapshot = self._snapshot
            config = dict(self._config)
        return {
            "enabled": self.get_state(), "host": config["host"],
            "proxy": bool(config["proxy"]), "cron": config["cron"],
            "indexers": len(snapshot[0]), "available": len(snapshot[1]),
            "selected_indexers": list(config["selected_indexers"]),
            "search_timeout": config["search_timeout"], "authoritative": snapshot[2],
            "indexer_names": [item["name"] for item in snapshot[0]],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        catalog = self._snapshot[1]
        fields = [
            ("VSwitch", {"model": "enabled", "label": "启用插件"}, 4),
            ("VSwitch", {"model": "proxy", "label": "使用代理服务器"}, 4),
            ("VSwitch", {"model": "onlyonce", "label": "立即运行一次",
                         "hint": "打开后立即获取索引器列表"}, 6),
            ("VTextField", {"model": "cron", "label": "更新周期",
                            "placeholder": self._DEFAULT_CRON,
                            "hint": "索引列表更新周期，支持5位cron表达式，默认每天零点运行一次"}, 6),
            ("VTextField", {"model": "host", "label": "Prowlarr地址",
                            "placeholder": "http://127.0.0.1:9696",
                            "hint": "Prowlarr访问地址和端口，如为https需加https://前缀"}, 6),
            ("VTextField", {"model": "api_key", "label": "Api Key",
                            "hint": "在Prowlarr->Settings->General->Security->API Key中获取"}, 6),
            ("VTextField", {"model": "search_timeout", "label": "搜索超时（秒）",
                            "type": "number", "placeholder": str(DEFAULT_SEARCH_TIMEOUT),
                            "hint": "整轮搜索总超时，包含目录获取与聚合检索，范围 5-120 秒"}, 6),
            ("VSelect", {"model": "selected_indexers", "label": "桥接索引器",
                         "items": selection.indexer_select_items(catalog),
                         "multiple": True, "chips": True, "clearable": True,
                         "hint": "留空表示搜索全部可用索引器；先立即运行一次拉取列表后再多选"}, 12),
        ]
        columns = [
            {"component": "VCol", "props": {"cols": 12, "md": width},
             "content": [{"component": component, "props": props}]}
            for component, props, width in fields
        ]
        columns.append({
            "component": "VCol", "props": {"cols": 12},
            "content": [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": "请在 Prowlarr 添加可搜索的 Torrent 索引器并填写地址与 API Key。"
                        "多选可限制搜索范围。连接测试：/api/v1/plugin/ProwlarrExtend/test",
            }}],
        })
        return [{"component": "VForm", "content": [
            {"component": "VRow", "content": columns}
        ]}], self._defaults()

    def get_page(self) -> List[dict]:
        indexers, catalog, authoritative = self._snapshot
        summary = f"当前选择 {len(indexers)} 个索引器，目录 {len(catalog)} 个"
        if not authoritative:
            summary += "；尚未获取有效目录，请检查连接后刷新"
        elif not indexers:
            summary += "；当前没有可用 Torrent 索引器，请检查多选与 Prowlarr 配置"
        page = [{"component": "VAlert", "props": {
            "type": "info" if indexers else "warning", "variant": "tonal", "text": summary,
        }}]
        if indexers:
            page.append({"component": "VTable", "props": {"hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": title}
                    for title in ("索引器名称", "索引器 ID", "是否公开")
                ]}]},
                {"component": "tbody", "content": [
                    {"component": "tr", "content": [
                        {"component": "td", "text": item["name"]},
                        {"component": "td", "text": item["id"]},
                        {"component": "td", "text": "是" if item["privacy"] == "public" else "否"},
                    ]} for item in indexers
                ]},
            ]})
        return page
