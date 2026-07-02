# _*_ coding: utf-8 _*_
import copy
import traceback
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlencode, quote_plus

from apscheduler.triggers.cron import CronTrigger
from app.helper.sites import SitesHelper

from app.core.context import TorrentInfo
from app.plugins import _PluginBase
from app.core.config import settings
from app.schemas import MediaType
from app.schemas.types import SystemConfigKey
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
    plugin_version = "2.1"
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
    # 虚拟站点域名后缀，索引器 ID 作为子域，形如 "15.prowlarr.extend"
    prowlarr_domain = "prowlarr.extend"

    def init_plugin(self, config: dict = None):
        self.sites_helper = SitesHelper()
        self._indexers = []
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
            self._cron = config.get("cron") or "0 0 */24 * *"

        if self._onlyonce:
            logger.info(f"【{self.plugin_name}】立即获取索引器状态")
            self.get_status()
            self._onlyonce = False
            self.__update_config()

        if not self._indexers:
            self.get_status()

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
        self.__sync_search_sites()
        logger.info(
            f"【{self.plugin_name}】索引器加载完成，共 {len(self._indexers)} 个，"
            f"本次注册 {registered} 个、更新 {updated} 个虚拟站点"
        )

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "ProwlarrExtendRefresh",
                "name": "Prowlarr 索引刷新",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.get_status,
                "kwargs": {}
            }]
        return []

    def get_status(self):
        if not self._api_key or not self._host:
            logger.warning(
                f"【{self.plugin_name}】get_status 提前返回："
                f"host={'有' if self._host else '空'}, "
                f"api_key={'有' if self._api_key else '空'}"
            )
            return False
        self._indexers = self.get_indexers()
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
        }

    def get_indexers(self):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": settings.USER_AGENT,
            "X-Api-Key": self._api_key,
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }
        indexer_query_url = f"{self._host}/api/v1/indexerstats"
        try:
            ret = RequestUtils(
                headers=headers,
                proxies=settings.PROXY if self._proxy else None
            ).get_res(indexer_query_url)
            if not ret:
                logger.warning(f"【{self.plugin_name}】获取 indexer 请求无响应")
                return []

            data = ret.json()
            if not data or "indexers" not in data:
                logger.warning(f"【{self.plugin_name}】返回数据不包含 indexers 字段")
                return []

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

                indexers.append({
                    "id": f'{self.plugin_name}-{indexer_id}',
                    "name": f'{self.plugin_name}-{indexer_name}',
                    "url": f'{self._host}/api/v1/indexer/{indexer_id}',
                    "domain": f'{indexer_id}.{self.prowlarr_domain}',
                    "public": True,
                    "proxy": self._proxy,
                })

            logger.info(f"【{self.plugin_name}】从 Prowlarr 获取到 {len(indexers)} 个索引器")
            return indexers
        except Exception as e:
            logger.error(f"【{self.plugin_name}】获取 indexer 失败：{str(e)}")
            return []

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
                         ("limit", 150),
                         ("offset", page * 150 if page else 0),
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
                torrent = TorrentInfo(
                    title=entry.get("title"),
                    enclosure=entry.get("downloadUrl") or entry.get("magnetUrl"),
                    description=entry.get("sortTitle"),
                    size=entry.get("size"),
                    seeders=entry.get("seeders"),
                    peers=entry.get("leechers"),
                    grabs=entry.get("grabs"),
                    pubdate=entry.get("publishDate"),
                    page_url=entry.get("infoUrl") or entry.get("guid"),
                    site_name=site_name,
                    labels=cat_labels,
                    category=self._infer_category(entry.get("categories", [])),
                    downloadvolumefactor=1.0,
                    uploadvolumefactor=1.0,
                )
                results.append(torrent)

        except Exception as e:
            logger.error(f"【{self.plugin_name}】检索错误：{str(e)}\n{traceback.format_exc()}")

        return results

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
        # 域名形如 "15.prowlarr.extend"，索引器 ID 是第一段。
        raw_domain = site.get("domain", "")
        domain_indexer_id = raw_domain.split(".")[0] if raw_domain else ""
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

    def __sync_search_sites(self):
        if not self._enabled or not self._indexers:
            return

        selected_sites = self.systemconfig.get(SystemConfigKey.IndexerSites) or []
        if not selected_sites:
            return

        prowlarr_ids = [
            indexer.get("id")
            for indexer in self._indexers
            if indexer.get("id")
        ]
        if not prowlarr_ids:
            return

        cleaned_sites = [
            site_id
            for site_id in selected_sites
            if not (isinstance(site_id, str) and site_id.startswith(f"{self.plugin_name}-"))
        ]
        missing_ids = [
            site_id
            for site_id in prowlarr_ids
            if site_id not in cleaned_sites
        ]
        if not missing_ids and cleaned_sites == selected_sites:
            return

        self.systemconfig.set(SystemConfigKey.IndexerSites, cleaned_sites + missing_ids)
        logger.info(f"【{self.plugin_name}】已同步 {len(prowlarr_ids)} 个虚拟站点到搜索站点范围")

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
                                            'placeholder': '0 0 */24 * *',
                                            'hint': '索引列表更新周期，支持5位cron表达式，默认每24小时运行一次'
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
            "cron": "0 0 */24 * *",
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
