# _*_ coding: utf-8 _*_
import copy
import re
import traceback
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlencode, quote_plus, urlparse

from apscheduler.triggers.cron import CronTrigger
from fastapi.concurrency import run_in_threadpool
from app.helper.sites import SitesHelper

from app.core.context import TorrentInfo
from app.db.models.site import Site
from app.plugins import _PluginBase
from app.core.config import settings
from app.schemas import MediaType
from app.schemas.types import EventType, SystemConfigKey
from app.utils.http import RequestUtils
from app.log import logger


class ProwlarrExtend(_PluginBase):
    # 插件名称
    plugin_name = "ProwlarrExtend"
    # 插件描述
    plugin_desc = "扩展检索以支持Prowlarr站点资源"
    # 插件图标
    plugin_icon = "Prowlarr.png"
    # 插件版本
    plugin_version = "2.4"
    # 插件作者
    plugin_author = "milikii"
    # 作者主页
    author_url = "https://github.com/milikii"
    # 插件配置项ID前缀
    plugin_config_prefix = "prowlarr_extend_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1
    # 虚拟站点域名，索引器 ID 放在主域中，避免 MoviePilot 将数字子域规范化掉。
    prowlarr_domain_suffix = "extend"
    _DEFAULT_CRON = "0 0 * * *"
    _LEGACY_CRONS = {"0 0 */24 * *"}
    _SEARCH_PAGE_SIZE = 100

    def init_plugin(self, config: dict = None):
        config_changed = False
        self.sites_helper = SitesHelper()
        self._indexers = []
        self._indexers_authoritative = False
        self._cron = None
        self._enabled = False
        self._proxy = False
        self._host = ""
        self._api_key = ""
        self._onlyonce = False

        if config:
            self._host = config.get("host", "")
            if self._host:
                if not self._host.startswith('http'):
                    self._host = "http://" + self._host
                if self._host.endswith('/'):
                    self._host = self._host.rstrip('/')
            self._api_key = config.get("api_key", "")
            self._enabled = config.get("enabled", False)
            self._proxy = config.get("proxy", False)
            self._onlyonce = config.get("onlyonce", False)
            configured_cron = config.get("cron")
            self._cron = configured_cron or self._DEFAULT_CRON
            if configured_cron in self._LEGACY_CRONS:
                self._cron = self._DEFAULT_CRON
                config_changed = True

        if config_changed and not self._onlyonce:
            logger.info(
                f"【{self.plugin_name}】已将错误的旧更新周期迁移为每天零点：{self._cron}"
            )
            self.__update_config()

        synced_once = False
        if self._onlyonce:
            logger.info(f"【{self.plugin_name}】立即获取索引器状态")
            self.sync_indexers()
            synced_once = True
            self._onlyonce = False
            self.__update_config()

        if not synced_once and not self._indexers:
            self.sync_indexers()

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "ProwlarrExtendRefresh",
                "name": "Prowlarr 索引刷新",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_indexers,
                "kwargs": {}
            }]
        return []

    def get_status(self):
        return self.sync_indexers()

    def sync_indexers(self):
        if not self._api_key or not self._host:
            logger.warning(
                f"【{self.plugin_name}】同步索引器提前返回："
                f"host={'有' if self._host else '空'}, "
                f"api_key={'有' if self._api_key else '空'}"
            )
            return False
        indexers = self.get_indexers()
        if indexers is None:
            logger.warning(f"【{self.plugin_name}】本次索引器同步失败，保留 MoviePilot 现有站点")
            return False
        self._indexers = indexers
        if not self._enabled:
            return True if isinstance(self._indexers, list) and len(self._indexers) > 0 else False

        registered, updated = self.__sync_helper_indexers()
        site_ids, removed_site_ids = self.__sync_site_records()
        self.__sync_search_sites(site_ids, removed_site_ids)
        logger.info(
            f"【{self.plugin_name}】索引器同步完成，当前启用 {len(self._indexers)} 个，"
            f"本次注册 {registered} 个、更新 {updated} 个虚拟站点"
        )
        return True if isinstance(self._indexers, list) and len(self._indexers) > 0 else False

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        pass

    def __update_config(self):
        saved_config = self.get_config() or {}
        host = self._host or saved_config.get("host", "")
        api_key = self._api_key or saved_config.get("api_key", "")
        if (not self._host or not self._api_key) and (saved_config.get("host") or saved_config.get("api_key")):
            logger.warning(f"【{self.plugin_name}】当前 Prowlarr 配置为空，保留已保存的 host/api_key，避免覆盖有效配置")

        self.update_config({
            "onlyonce": False,
            "cron": self._cron,
            "host": host,
            "api_key": api_key,
            "enabled": self._enabled,
            "proxy": self._proxy,
        })

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_module(self) -> Dict[str, Any]:
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    def __headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": settings.USER_AGENT,
            "X-Api-Key": self._api_key,
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }

    def __request_json(self, api_url: str):
        ret = RequestUtils(
            headers=self.__headers(),
            proxies=settings.PROXY if self._proxy else None
        ).get_res(api_url)
        if not ret:
            return None
        return ret.json()

    def get_indexers(self):
        self._indexers_authoritative = False
        indexers = self.__get_indexers_from_config()
        if indexers is not None:
            self._indexers_authoritative = True
            return indexers
        return self.__get_indexers_from_stats()

    def __get_indexers_from_config(self):
        indexer_query_url = f"{self._host}/api/v1/indexer"
        try:
            data = self.__request_json(indexer_query_url)
            if data is None:
                logger.warning(f"【{self.plugin_name}】获取 indexer 配置请求无响应")
                return None
            if not isinstance(data, list):
                logger.warning(f"【{self.plugin_name}】indexer 配置返回数据格式异常")
                return None
            if not data:
                logger.info(f"【{self.plugin_name}】Prowlarr 未配置任何 indexer")
                return []

            indexers = []
            disabled = 0
            unsupported = 0
            for item in data:
                indexer_id = item.get("id")
                indexer_name = item.get("name") or item.get("definitionName")
                if not indexer_id or not indexer_name:
                    continue
                if item.get("enable") is False:
                    disabled += 1
                    continue
                if item.get("supportsSearch") is False:
                    unsupported += 1
                    continue

                protocol = item.get("protocol")
                if isinstance(protocol, str) and protocol.lower() != "torrent":
                    unsupported += 1
                    continue

                indexers.append(self.__build_indexer(
                    indexer_id,
                    indexer_name,
                    privacy=item.get("privacy"),
                ))

            logger.info(
                f"【{self.plugin_name}】从 Prowlarr 获取到 {len(indexers)} 个启用索引器，"
                f"跳过禁用 {disabled} 个、不支持搜索/非 Torrent {unsupported} 个"
            )
            return indexers
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】获取 indexer 配置失败，将回退到 indexerstats：{str(e)}")
            return None

    def __get_indexers_from_stats(self):
        indexer_query_url = f"{self._host}/api/v1/indexerstats"
        try:
            data = self.__request_json(indexer_query_url)
            if data is None:
                logger.warning(f"【{self.plugin_name}】获取 indexer 请求无响应")
                return None

            if not data or "indexers" not in data:
                logger.warning(f"【{self.plugin_name}】返回数据不包含 indexers 字段")
                return None

            indexers_raw = data.get("indexers", [])
            if not indexers_raw:
                logger.info(f"【{self.plugin_name}】未配置任何 indexer")
                return []

            indexers = []
            for v in indexers_raw:
                indexer_id = v.get("indexerId")
                indexer_name = v.get("indexerName")
                if not indexer_id or not indexer_name:
                    continue

                indexers.append(self.__build_indexer(indexer_id, indexer_name))

            logger.info(f"【{self.plugin_name}】从 Prowlarr indexerstats 获取到 {len(indexers)} 个索引器")
            return indexers
        except Exception as e:
            logger.error(f"【{self.plugin_name}】获取 indexer 失败：{str(e)}")
            return None

    def __build_indexer(self, indexer_id, indexer_name, privacy: Optional[str] = None) -> Dict[str, Any]:
        privacy_value = str(privacy or "").strip().lower()
        return {
            "id": f'{self.plugin_name}-{indexer_id}',
            "name": f'{self.plugin_name}-{indexer_name}',
            "url": f'{self._host}/api/v1/indexer/{indexer_id}',
            "domain": self.__build_domain(indexer_id),
            "public": privacy_value == "public",
            "privacy": privacy_value or "unknown",
            "proxy": self._proxy,
            "result_num": self._SEARCH_PAGE_SIZE,
        }

    def search_torrents(self, site: dict, keyword: str, mtype: Optional[MediaType] = None, page: Optional[int] = 0) -> \
            List[TorrentInfo]:
        results = []

        if not site or not keyword:
            return results

        if site.get("name", "").split("-")[0] != self.plugin_name:
            return results

        indexer_id = self.__get_indexer_id(site)
        if not indexer_id or not indexer_id.isdigit():
            logger.warning(f"【{self.plugin_name}】无法提取索引 ID，跳过站点：{site.get('name')}（domain={site.get('domain')}）")
            return results

        site_name = site.get("name", "").replace(f"{self.plugin_name}-", "", 1)

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": settings.USER_AGENT,
            "X-Api-Key": self._api_key,
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }
        categories = self.get_cat(mtype)
        try:
            logger.info(f"【{self.plugin_name}】开始检索 Indexer：{site.get('name')}，关键词：{keyword}")
            params = [
                         ("query", keyword),
                         ("indexerIds", indexer_id),
                         ("type", "search"),
                         ("limit", self._SEARCH_PAGE_SIZE),
                         ("offset", page * self._SEARCH_PAGE_SIZE if page else 0),
                     ] + [("categories", cat) for cat in categories]
            query_string = urlencode(params, quote_via=quote_plus)
            api_url = f"{self._host}/api/v1/search?{query_string}"

            response = RequestUtils(
                headers=headers,
                proxies=settings.PROXY if self._proxy else None
            ).get_res(api_url)
            if not response:
                logger.warning(f"【{self.plugin_name}】{site.get('name')} 返回为空")
                return results

            data = response.json()
            if not isinstance(data, list):
                logger.warning(f"【{self.plugin_name}】{site.get('name')} 返回数据格式异常")
                return results

            for entry in data:
                cat_labels = [c.get("name") for c in entry.get("categories", []) if c.get("name")]
                download_factor, upload_factor = self._volume_factors(entry)
                torrent = TorrentInfo(
                    title=entry.get("title"),
                    enclosure=entry.get("downloadUrl") or entry.get("magnetUrl"),
                    description=entry.get("sortTitle"),
                    size=entry.get("size"),
                    seeders=entry.get("seeders"),
                    peers=entry.get("leechers"),
                    grabs=entry.get("grabs"),
                    pubdate=self._normalize_pubdate(entry.get("publishDate")),
                    page_url=entry.get("infoUrl") or entry.get("guid"),
                    imdbid=self._normalize_imdb_id(entry.get("imdbId")),
                    site_name=site_name,
                    labels=cat_labels,
                    category=self._infer_category(entry.get("categories", [])),
                    downloadvolumefactor=download_factor,
                    uploadvolumefactor=upload_factor,
                )
                results.append(torrent)

        except Exception as e:
            logger.error(f"【{self.plugin_name}】检索错误：{str(e)}\n{traceback.format_exc()}")

        return results

    @staticmethod
    def _normalize_pubdate(value: Any) -> Any:
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

    @staticmethod
    def _normalize_imdb_id(value: Any) -> Optional[str]:
        if value in (None, "", 0, "0"):
            return None
        raw_value = str(value).strip()
        if raw_value.lower().startswith("tt"):
            return raw_value
        if raw_value.isdigit():
            return f"tt{int(raw_value):07d}"
        return raw_value

    @staticmethod
    def _volume_factors(entry: Dict[str, Any]) -> Tuple[float, float]:
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

    async def async_search_torrents(self, site: dict, keyword: str, mtype: Optional[MediaType] = None,
                                    page: Optional[int] = 0) -> List[TorrentInfo]:
        return await run_in_threadpool(
            self.search_torrents,
            site=site,
            keyword=keyword,
            mtype=mtype,
            page=page,
        )

    @staticmethod
    def _infer_category(categories: list) -> str:
        for cat in categories:
            cat_id = cat.get("id", 0)
            if 2000 <= cat_id < 3000:
                return "电影"
            elif 5000 <= cat_id < 6000:
                return "电视剧"
        return ""

    @staticmethod
    def get_cat(mtype: Optional[MediaType] = None):
        if not mtype:
            return [2000, 5000]
        elif mtype == MediaType.MOVIE:
            return [2000]
        elif mtype == MediaType.TV:
            return [5000]
        else:
            return [2000, 5000]

    def __get_indexer_id(self, site: dict) -> str:
        # 新域名形如 "prowlarr-15.extend"，兼容旧域名 "15.prowlarr.extend"。
        raw_domain = site.get("domain", "")
        if raw_domain and "://" in raw_domain:
            raw_domain = urlparse(raw_domain).hostname or raw_domain
        raw_domain = raw_domain.strip("/")
        if raw_domain.startswith("prowlarr-"):
            domain_indexer_id = raw_domain.split(".", 1)[0].replace("prowlarr-", "", 1)
            if domain_indexer_id.isdigit():
                return domain_indexer_id
        if raw_domain:
            domain_indexer_id = raw_domain.split(".")[0]
            if domain_indexer_id.isdigit():
                return domain_indexer_id

        site_id = str(site.get("id", ""))
        id_prefix = f"{self.plugin_name}-"
        if site_id.startswith(id_prefix):
            id_indexer_id = site_id.replace(id_prefix, "", 1)
            if id_indexer_id.isdigit():
                return id_indexer_id

        url = site.get("url", "")
        if "/indexer/" in url:
            url_indexer_id = url.rstrip("/").rsplit("/", 1)[-1]
            if url_indexer_id.isdigit():
                return url_indexer_id

        return ""

    def __build_domain(self, indexer_id) -> str:
        return f"prowlarr-{indexer_id}.{self.prowlarr_domain_suffix}"

    def __legacy_domain(self, indexer_id) -> str:
        return f"{indexer_id}.prowlarr.extend"

    def __is_managed_domain(self, domain: str) -> bool:
        if not domain:
            return False
        raw_domain = domain
        if "://" in raw_domain:
            raw_domain = urlparse(raw_domain).hostname or raw_domain
        raw_domain = raw_domain.strip("/")

        if raw_domain.startswith("prowlarr-") and raw_domain.endswith(f".{self.prowlarr_domain_suffix}"):
            indexer_id = raw_domain.split(".", 1)[0].replace("prowlarr-", "", 1)
            return indexer_id.isdigit()

        parts = raw_domain.split(".")
        return len(parts) == 3 and parts[0].isdigit() and parts[1:] == ["prowlarr", "extend"]

    def __get_managed_site_records(self) -> List[Site]:
        try:
            sites = Site.list_order_by_pri(None) or []
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】读取 MoviePilot 站点列表失败，跳过旧站点清理：{str(e)}")
            return []
        return [site for site in sites if self.__is_managed_domain(getattr(site, "domain", ""))]

    def __sync_helper_indexers(self) -> Tuple[int, int]:
        registered = 0
        updated = 0
        for indexer in self._indexers:
            domain = indexer.get("domain", "")
            site_info = self.sites_helper.get_indexer(domain)
            if not site_info:
                self.sites_helper.add_indexer(domain, copy.deepcopy(indexer))
                registered += 1
            elif site_info.get("id") != indexer.get("id") or site_info.get("url") != indexer.get("url"):
                self.sites_helper.add_indexer(domain, copy.deepcopy(indexer))
                updated += 1
        return registered, updated

    def __sync_site_records(self) -> Tuple[List[int], List[int]]:
        if not self._enabled:
            return [], []

        current_domains = {indexer.get("domain") for indexer in self._indexers if indexer.get("domain")}
        site_ids = []
        removed_site_ids = []
        removed_site_keys = set()
        created = 0
        updated = 0
        removed = 0

        for indexer in self._indexers:
            domain = indexer.get("domain")
            if not domain:
                continue

            payload = {
                "name": indexer.get("name"),
                "domain": domain,
                "url": f"https://{domain}/",
                "pri": 0,
                "public": 1 if indexer.get("public") else 0,
                "proxy": 1 if self._proxy else 0,
                "render": 0,
                "timeout": 15,
                "is_active": True,
            }

            site = Site.get_by_domain(None, domain)
            if not site:
                Site(**payload).create(None)
                site = Site.get_by_domain(None, domain)
                created += 1
            else:
                update_payload = {
                    key: value
                    for key, value in payload.items()
                    if getattr(site, key, None) != value
                }
                if update_payload:
                    site.update(None, update_payload)
                    site = Site.get_by_domain(None, domain)
                    updated += 1

            if site and site.id:
                site_ids.append(site.id)

            indexer_id = self.__get_indexer_id(indexer)
            legacy_site = Site.get_by_domain(None, self.__legacy_domain(indexer_id)) if indexer_id else None
            legacy_site_id = getattr(legacy_site, "id", None)
            if legacy_site and legacy_site_id and legacy_site.domain not in current_domains:
                Site.delete(None, legacy_site.id)
                removed_site_ids.append(legacy_site_id)
                removed_site_keys.add(str(legacy_site_id))
                removed += 1

        if self._indexers_authoritative:
            for site in self.__get_managed_site_records():
                domain = getattr(site, "domain", "")
                site_id = getattr(site, "id", None)
                if site_id and str(site_id) not in removed_site_keys and domain not in current_domains:
                    Site.delete(None, site_id)
                    removed_site_ids.append(site_id)
                    removed_site_keys.add(str(site_id))
                    removed += 1
        else:
            logger.warning(
                f"【{self.plugin_name}】当前使用 indexerstats 降级快照，"
                "仅新增或更新站点，不清理缺失站点"
            )

        if created or updated or removed:
            self.eventmanager.send_event(EventType.SiteUpdated, {"plugin_id": self.plugin_name})
            logger.info(
                f"【{self.plugin_name}】同步正式站点：新增 {created} 个、更新 {updated} 个、清理旧站点 {removed} 个"
            )

        return site_ids, removed_site_ids

    def __sync_search_sites(self, site_ids: List[int], removed_site_ids: Optional[List[int]] = None):
        if not self._enabled:
            return

        selected_sites = self.systemconfig.get(SystemConfigKey.IndexerSites) or []
        if not selected_sites:
            return

        removed_site_ids = removed_site_ids or []
        managed_site_keys = {str(site_id) for site_id in site_ids + removed_site_ids if site_id is not None}

        cleaned_sites = [
            site_id
            for site_id in selected_sites
            if not (isinstance(site_id, str) and site_id.startswith(f"{self.plugin_name}-"))
            and str(site_id) not in managed_site_keys
        ]
        missing_ids = [
            site_id
            for site_id in site_ids
            if str(site_id) not in {str(cleaned_site) for cleaned_site in cleaned_sites}
        ]
        if not missing_ids and cleaned_sites == selected_sites:
            return

        self.systemconfig.set(SystemConfigKey.IndexerSites, cleaned_sites + missing_ids)
        logger.info(
            f"【{self.plugin_name}】已同步 {len(site_ids)} 个正式站点到搜索站点范围，"
            f"清理 {len(removed_site_ids)} 个旧站点"
        )

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'proxy',
                                            'label': '使用代理服务器',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                            'hint': '打开后立即运行一次获取索引器列表，否则需要等到预先设置的更新周期才会获取'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '更新周期',
                                            'placeholder': '0 0 * * *',
                                            'hint': '索引列表更新周期，支持5位cron表达式，默认每天零点运行一次'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'host',
                                            'label': 'Prowlarr地址',
                                            'placeholder': 'http://127.0.0.1:9696',
                                            'hint': 'Prowlarr访问地址和端口，如为https需加https://前缀'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'api_key',
                                            'label': 'Api Key',
                                            'placeholder': '',
                                            'hint': '在Prowlarr->Settings->General->Security->API Key中获取'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '请先在Prowlarr中添加索引器并确保其正常工作，然后在此配置地址和API Key即可。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "host": "",
            "api_key": "",
            "cron": "0 0 * * *",
            "enabled": False,
            "proxy": False,
            "onlyonce": False
        }

    def _ensure_sites_loaded(self) -> bool:
        if isinstance(self._indexers, list) and len(self._indexers) > 0:
            return True
        self.get_status()
        return isinstance(self._indexers, list) and len(self._indexers) > 0

    def get_page(self) -> List[dict]:
        if not self._ensure_sites_loaded():
            return []

        items = []
        for site in self._indexers:
            items.append({
                'component': 'tr',
                'content': [
                    {
                        'component': 'td',
                        'text': site.get("name")
                    },
                    {
                        'component': 'td',
                        'text': f"https://{site.get('domain')}"
                    },
                    {
                        'component': 'td',
                        'text': site.get("public")
                    }
                ]
            })

        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'content': [
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-start ps-4'
                                                        },
                                                        'text': 'id'
                                                    },
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-start ps-4'
                                                        },
                                                        'text': '站点名称'
                                                    },
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-start ps-4'
                                                        },
                                                        'text': '是否公开'
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': items
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
