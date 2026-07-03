# _*_ coding: utf-8 _*_
import re
import threading
import traceback
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.context import MediaInfo, SubtitleInfo
from app.core.event import eventmanager, Event
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class SubtitleHunter(_PluginBase):
    plugin_name = "SubtitleHunter"
    plugin_desc = "资源入库后自动遍历所有PT站点搜索并下载中文字幕"
    plugin_icon = "subtitle.png"
    plugin_version = "1.1"
    plugin_author = "milikii"
    author_url = "https://github.com/milikii"
    plugin_config_prefix = "subtitle_hunter_"
    plugin_order = 20
    auth_level = 1

    _CHI_PATTERNS = re.compile(
        r"(chi|chs|cht|chinese|中文|简体|繁体|简中|繁中|双语|中英)",
        re.IGNORECASE,
    )

    def init_plugin(self, config: dict = None):
        self._enabled = False
        self._notify = True
        self._onlyonce = False
        self._target_path = ""
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._target_path = config.get("target_path", "")

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": False,
                "target_path": self._target_path,
            })
            if self._target_path:
                threading.Thread(
                    target=self._run_once,
                    args=(self._target_path,),
                    daemon=True,
                    name="SubtitleHunter-RunOnce",
                ).start()
            else:
                logger.warning(f"【{self.plugin_name}】运行一次：未指定媒体目录路径")

    def _run_once(self, target_path: str):
        """
        手动运行一次：对指定媒体目录搜索中文字幕。
        """
        target_dir = Path(target_path)
        logger.info(f"【{self.plugin_name}】手动运行：目标目录 {target_dir}")

        if not target_dir.exists():
            logger.error(f"【{self.plugin_name}】目标目录不存在：{target_dir}")
            return

        if self._has_chinese_subtitle(target_dir):
            logger.info(f"【{self.plugin_name}】{target_dir.name} 已有中文字幕，跳过")
            return

        logger.info(f"【{self.plugin_name}】{target_dir.name} 未找到中文字幕，开始全站搜索...")

        keyword = target_dir.name
        # 尝试从 nfo 文件提取 IMDB ID
        for nfo in target_dir.glob("*.nfo"):
            try:
                content = nfo.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"(tt\d{7,})", content)
                if match:
                    keyword = match.group(1)
                    logger.info(f"【{self.plugin_name}】从 nfo 提取到 IMDB ID：{keyword}")
                    break
            except Exception:
                pass

        sites = SitesHelper().get_indexers()
        if not sites:
            logger.warning(f"【{self.plugin_name}】无可用站点")
            return

        logger.info(f"【{self.plugin_name}】搜索关键词：{keyword}，站点数：{len(sites)}")

        search_chain = SearchChain()
        all_subtitles: List[SubtitleInfo] = []

        for site in sites:
            site_name = site.get("name", "未知")
            try:
                results = search_chain.search_subtitles(
                    site=site, keyword=keyword, page=0
                )
                if results:
                    logger.info(f"【{self.plugin_name}】{site_name} 返回 {len(results)} 条字幕")
                    all_subtitles.extend(results)
            except Exception as e:
                logger.debug(f"【{self.plugin_name}】{site_name} 搜索字幕异常：{e}")

        chinese_subs = [s for s in all_subtitles if self._is_chinese_subtitle(s)]

        if not chinese_subs:
            logger.info(
                f"【{self.plugin_name}】全站搜索完成，共 {len(all_subtitles)} 条字幕，无中文字幕"
            )
            return

        chinese_subs.sort(key=lambda s: s.grabs or 0, reverse=True)
        best = chinese_subs[0]

        logger.info(
            f"【{self.plugin_name}】找到 {len(chinese_subs)} 条中文字幕，"
            f"选择：{best.title}（来源：{best.site_name}，下载次数：{best.grabs}）"
        )

        self._download_subtitle(best, target_dir)

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_module(self) -> Dict[str, Any]:
        return {}

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled:
            return

        event_data = event.event_data or {}
        mediainfo: MediaInfo = event_data.get("mediainfo")
        transferinfo = event_data.get("transferinfo")

        if not mediainfo or not transferinfo:
            return

        target_path = transferinfo.target_path
        if not target_path:
            return

        target_dir = Path(target_path).parent if Path(target_path).is_file() else Path(target_path)

        threading.Thread(
            target=self._search_and_download,
            args=(mediainfo, target_dir),
            daemon=True,
            name=f"SubtitleHunter-{mediainfo.title_year}",
        ).start()

    def _search_and_download(self, mediainfo: MediaInfo, target_dir: Path):
        try:
            if self._has_chinese_subtitle(target_dir):
                logger.info(f"【{self.plugin_name}】{mediainfo.title_year} 已有中文字幕，跳过")
                return

            logger.info(f"【{self.plugin_name}】{mediainfo.title_year} 未找到中文字幕，开始全站搜索...")

            keyword = mediainfo.imdb_id or mediainfo.title
            if not keyword:
                logger.warning(f"【{self.plugin_name}】无法确定搜索关键词，跳过")
                return

            sites = SitesHelper().get_indexers()
            if not sites:
                logger.warning(f"【{self.plugin_name}】无可用站点")
                return

            logger.info(f"【{self.plugin_name}】搜索关键词：{keyword}，站点数：{len(sites)}")

            search_chain = SearchChain()
            all_subtitles: List[SubtitleInfo] = []

            for site in sites:
                site_name = site.get("name", "未知")
                try:
                    results = search_chain.search_subtitles(
                        site=site, keyword=keyword, page=0
                    )
                    if results:
                        logger.info(f"【{self.plugin_name}】{site_name} 返回 {len(results)} 条字幕")
                        all_subtitles.extend(results)
                except Exception as e:
                    logger.debug(f"【{self.plugin_name}】{site_name} 搜索字幕异常：{e}")

            chinese_subs = [s for s in all_subtitles if self._is_chinese_subtitle(s)]

            if not chinese_subs:
                logger.info(
                    f"【{self.plugin_name}】{mediainfo.title_year} 全站搜索完成，"
                    f"共 {len(all_subtitles)} 条字幕，无中文字幕"
                )
                return

            chinese_subs.sort(key=lambda s: s.grabs or 0, reverse=True)
            best = chinese_subs[0]

            logger.info(
                f"【{self.plugin_name}】{mediainfo.title_year} 找到 {len(chinese_subs)} 条中文字幕，"
                f"选择：{best.title}（来源：{best.site_name}，下载次数：{best.grabs}）"
            )

            self._download_subtitle(best, target_dir)

        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】{mediainfo.title_year} 字幕搜索失败：{e}\n"
                f"{traceback.format_exc()}"
            )

    def _download_subtitle(self, subtitle: SubtitleInfo, target_dir: Path):
        if not subtitle.enclosure:
            logger.warning(f"【{self.plugin_name}】字幕无下载链接：{subtitle.title}")
            return

        download_chain = DownloadChain()
        storage_chain = StorageChain()

        working_dir_item, err = download_chain._get_subtitle_working_dir(
            storage_chain=storage_chain,
            storage="local",
            target_path=target_dir,
        )
        if not working_dir_item:
            logger.error(f"【{self.plugin_name}】{err}")
            return

        from app.utils.http import RequestUtils
        headers = {}
        if subtitle.site_cookie:
            headers["Cookie"] = subtitle.site_cookie
        if subtitle.site_ua:
            headers["User-Agent"] = subtitle.site_ua
        else:
            headers["User-Agent"] = settings.USER_AGENT

        proxies = settings.PROXY if subtitle.site_proxy else None

        try:
            response = RequestUtils(
                headers=headers, proxies=proxies
            ).get_res(subtitle.enclosure)

            if not response or response.status_code != 200:
                logger.warning(
                    f"【{self.plugin_name}】字幕下载失败：{subtitle.title}，"
                    f"状态码：{getattr(response, 'status_code', 'N/A')}"
                )
                return

            success, msg, files = download_chain._save_subtitle_response(
                subtitle=subtitle,
                response=response,
                storage="local",
                target_dir=target_dir,
            )

            if success:
                logger.info(f"【{self.plugin_name}】字幕下载成功：{subtitle.title} -> {files}")
            else:
                logger.warning(f"【{self.plugin_name}】字幕保存失败：{msg}")

        except Exception as e:
            logger.error(f"【{self.plugin_name}】字幕下载异常：{e}")

    def _has_chinese_subtitle(self, target_dir: Path) -> bool:
        if not target_dir.exists():
            return False

        sub_exts = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}
        for f in target_dir.iterdir():
            if f.suffix.lower() in sub_exts:
                stem_lower = f.stem.lower()
                if self._CHI_PATTERNS.search(stem_lower):
                    return True
        return False

    @classmethod
    def _is_chinese_subtitle(cls, subtitle: SubtitleInfo) -> bool:
        text = " ".join(filter(None, [
            subtitle.language or "",
            subtitle.title or "",
            subtitle.description or "",
            subtitle.file_name or "",
        ]))
        return bool(cls._CHI_PATTERNS.search(text))

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                            "hint": "保存后立即对指定目录搜索中文字幕",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "target_path",
                                            "label": "媒体目录路径",
                                            "placeholder": "/18T01/Movies/Movie/痴迷 (2026)",
                                            "hint": "运行一次时搜索该目录的中文字幕，填写媒体文件所在目录的完整路径",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "启用后资源入库自动搜索中文字幕。也可填写媒体目录后点击立即运行一次手动触发搜索。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "target_path": "",
        }

    def get_page(self) -> List[dict]:
        return []
