# _*_ coding: utf-8 _*_
import re
import threading
import time
import traceback
from datetime import datetime
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
    plugin_version = "1.2"
    plugin_author = "milikii"
    author_url = "https://github.com/milikii"
    plugin_config_prefix = "subtitle_hunter_"
    plugin_order = 20
    auth_level = 1

    _CHI_PATTERNS = re.compile(
        r"((^|[._\-\s\[\]()])"
        r"(chi|chs|cht|chinese|zh([_-]?(cn|hans|hant|tw|hk))?|zho|cmn)"
        r"(?=$|[._\-\s\[\]()]))|中文|简体|繁体|简中|繁中|双语|中英",
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

        self._init_runtime_status()

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": False,
                "target_path": self._target_path,
            })
            if self._target_path:
                logger.info(f"【{self.plugin_name}】已提交手动运行任务：{self._target_path}")
                threading.Thread(
                    target=self._run_once,
                    args=(self._target_path,),
                    daemon=True,
                    name="SubtitleHunter-RunOnce",
                ).start()
            else:
                message = "运行一次：未指定媒体目录路径"
                self._update_run(status="配置错误", message=message, error=message)
                logger.warning(f"【{self.plugin_name}】{message}")

    def _run_once(self, target_path: str):
        """
        手动运行一次：对指定媒体目录搜索中文字幕。
        """
        target_dir = self._resolve_target_dir(target_path)
        self._process_target(
            source="手动运行",
            target_dir=target_dir,
            display_name=target_dir.name or str(target_dir),
            keyword=target_dir.name,
            use_nfo=True,
        )

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/status",
            "endpoint": self.get_status,
            "methods": ["GET"],
            "auth": "apikey",
            "summary": "获取 SubtitleHunter 运行状态",
        }]

    def get_status(self) -> Dict[str, Any]:
        return self._runtime_snapshot()

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
            logger.warning(f"【{self.plugin_name}】入库完成事件缺少媒体信息或转移信息，跳过")
            return

        target_path = transferinfo.target_path
        if not target_path:
            logger.warning(f"【{self.plugin_name}】入库完成事件缺少目标路径，跳过")
            return

        target_dir = self._resolve_target_dir(target_path)
        title = mediainfo.title_year or mediainfo.title or target_dir.name
        logger.info(f"【{self.plugin_name}】收到入库完成事件：{title}，目标目录：{target_dir}")

        threading.Thread(
            target=self._search_and_download,
            args=(mediainfo, target_dir),
            daemon=True,
            name=f"SubtitleHunter-{title}",
        ).start()

    def _search_and_download(self, mediainfo: MediaInfo, target_dir: Path):
        title = mediainfo.title_year or mediainfo.title or target_dir.name
        keyword = mediainfo.imdb_id or mediainfo.title
        self._process_target(
            source="入库事件",
            target_dir=target_dir,
            display_name=title,
            keyword=keyword,
            use_nfo=False,
        )

    def _process_target(
        self,
        source: str,
        target_dir: Path,
        display_name: str,
        keyword: Optional[str],
        use_nfo: bool = False,
    ):
        started_at = self._start_run(
            source=source,
            target_dir=target_dir,
            display_name=display_name,
            keyword=keyword or "",
        )
        try:
            logger.info(f"【{self.plugin_name}】{source}：目标目录 {target_dir}")

            if not target_dir.exists():
                message = f"目标目录不存在：{target_dir}"
                logger.error(f"【{self.plugin_name}】{message}")
                self._finish_run("失败", message, started_at, error=message)
                return

            if not target_dir.is_dir():
                message = f"目标路径不是目录：{target_dir}"
                logger.error(f"【{self.plugin_name}】{message}")
                self._finish_run("失败", message, started_at, error=message)
                return

            if self._has_chinese_subtitle(target_dir):
                message = f"{display_name} 已有中文字幕，跳过"
                logger.info(f"【{self.plugin_name}】{message}")
                self._finish_run("已跳过", message, started_at)
                return

            logger.info(f"【{self.plugin_name}】{display_name} 未找到中文字幕，开始全站搜索...")

            if use_nfo:
                keyword = self._extract_imdb_id_from_nfo(target_dir) or keyword

            if not keyword:
                message = "无法确定搜索关键词，跳过"
                logger.warning(f"【{self.plugin_name}】{message}")
                self._finish_run("已跳过", message, started_at)
                return

            sites = SitesHelper().get_indexers()
            if not sites:
                message = "无可用站点"
                logger.warning(f"【{self.plugin_name}】{message}")
                self._finish_run("失败", message, started_at, keyword=keyword)
                return

            logger.info(f"【{self.plugin_name}】搜索关键词：{keyword}，站点数：{len(sites)}")
            self._update_run(
                keyword=keyword,
                sites=len(sites),
                message=f"正在搜索 {len(sites)} 个站点",
            )

            search_chain = SearchChain()
            all_subtitles: List[SubtitleInfo] = []
            site_errors = 0

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
                    site_errors += 1
                    logger.warning(f"【{self.plugin_name}】{site_name} 搜索字幕异常：{e}")

            chinese_subs = [s for s in all_subtitles if self._is_chinese_subtitle(s)]
            self._update_run(
                total_subtitles=len(all_subtitles),
                chinese_subtitles=len(chinese_subs),
                site_errors=site_errors,
            )

            if not chinese_subs:
                message = (
                    f"{display_name} 全站搜索完成，站点 {len(sites)} 个，"
                    f"共 {len(all_subtitles)} 条字幕，无中文字幕"
                )
                if site_errors:
                    message = f"{message}，{site_errors} 个站点异常"
                logger.info(f"【{self.plugin_name}】{message}")
                self._finish_run(
                    "未找到",
                    message,
                    started_at,
                    keyword=keyword,
                    sites=len(sites),
                    total_subtitles=len(all_subtitles),
                    chinese_subtitles=0,
                    site_errors=site_errors,
                )
                return

            chinese_subs.sort(key=self._subtitle_grabs, reverse=True)
            best = chinese_subs[0]
            selected = self._format_subtitle(best)

            logger.info(
                f"【{self.plugin_name}】{display_name} 找到 {len(chinese_subs)} 条中文字幕，"
                f"选择：{best.title}（来源：{best.site_name}，下载次数：{best.grabs}）"
            )
            self._update_run(
                selected=selected,
                message=f"找到 {len(chinese_subs)} 条中文字幕，正在下载",
            )

            success, message, files = self._download_subtitle(best, target_dir)
            status = "已下载" if success else "下载失败"
            self._finish_run(
                status,
                message,
                started_at,
                keyword=keyword,
                sites=len(sites),
                total_subtitles=len(all_subtitles),
                chinese_subtitles=len(chinese_subs),
                selected=selected,
                downloaded_files=", ".join(files),
                site_errors=site_errors,
                error="" if success else message,
            )

        except Exception as e:
            message = f"{display_name} 字幕搜索失败：{e}"
            logger.error(
                f"【{self.plugin_name}】{message}\n"
                f"{traceback.format_exc()}"
            )
            self._finish_run(
                "失败",
                message,
                started_at,
                error=traceback.format_exc(),
            )

    def _download_subtitle(self, subtitle: SubtitleInfo, target_dir: Path) -> Tuple[bool, str, List[str]]:
        if not subtitle.enclosure:
            message = f"字幕无下载链接：{subtitle.title}"
            logger.warning(f"【{self.plugin_name}】{message}")
            return False, message, []

        download_chain = DownloadChain()
        storage_chain = StorageChain()

        working_dir_item, err = download_chain._get_subtitle_working_dir(
            storage_chain=storage_chain,
            storage="local",
            target_path=target_dir,
        )
        if not working_dir_item:
            message = str(err)
            logger.error(f"【{self.plugin_name}】{message}")
            return False, message, []

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
                message = (
                    f"字幕下载失败：{subtitle.title}，"
                    f"状态码：{getattr(response, 'status_code', 'N/A')}"
                )
                logger.warning(f"【{self.plugin_name}】{message}")
                return False, message, []

            success, msg, files = download_chain._save_subtitle_response(
                subtitle=subtitle,
                response=response,
                storage="local",
                target_dir=target_dir,
            )

            if success:
                message = f"字幕下载成功：{subtitle.title} -> {files}"
                logger.info(f"【{self.plugin_name}】{message}")
                return True, message, [str(file) for file in files]
            else:
                message = f"字幕保存失败：{msg}"
                logger.warning(f"【{self.plugin_name}】{message}")
                return False, message, []

        except Exception as e:
            message = f"字幕下载异常：{e}"
            logger.error(f"【{self.plugin_name}】{message}")
            return False, message, []

    def _extract_imdb_id_from_nfo(self, target_dir: Path) -> Optional[str]:
        for nfo in target_dir.glob("*.nfo"):
            try:
                content = nfo.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"(tt\d{7,})", content)
                if match:
                    keyword = match.group(1)
                    logger.info(f"【{self.plugin_name}】从 nfo 提取到 IMDB ID：{keyword}")
                    return keyword
            except Exception as e:
                logger.warning(f"【{self.plugin_name}】读取 nfo 失败：{nfo}，{e}")
        return None

    def _has_chinese_subtitle(self, target_dir: Path) -> bool:
        if not target_dir.exists() or not target_dir.is_dir():
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

    @staticmethod
    def _resolve_target_dir(target_path: str) -> Path:
        path = Path(target_path)
        return path.parent if path.is_file() else path

    @staticmethod
    def _subtitle_grabs(subtitle: SubtitleInfo) -> int:
        try:
            return int(subtitle.grabs or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _format_subtitle(subtitle: SubtitleInfo) -> str:
        site_name = subtitle.site_name or "未知来源"
        grabs = subtitle.grabs if subtitle.grabs is not None else 0
        return f"{subtitle.title}（{site_name}，下载次数：{grabs}）"

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _init_runtime_status(self):
        self._status_lock = threading.Lock()
        self._runtime = {
            "running": False,
            "status": "未运行",
            "source": "-",
            "started_at": "-",
            "finished_at": "-",
            "duration": "-",
            "target_path": self._target_path if hasattr(self, "_target_path") else "",
            "media": "-",
            "keyword": "-",
            "sites": 0,
            "total_subtitles": 0,
            "chinese_subtitles": 0,
            "site_errors": 0,
            "selected": "-",
            "downloaded_files": "-",
            "message": "暂无运行记录",
            "error": "",
        }
        self._history = []

    def _ensure_runtime_status(self):
        if not hasattr(self, "_status_lock"):
            self._init_runtime_status()

    def _start_run(
        self,
        source: str,
        target_dir: Path,
        display_name: str,
        keyword: str,
    ) -> float:
        self._ensure_runtime_status()
        started_at = time.monotonic()
        with self._status_lock:
            self._runtime.update({
                "running": True,
                "status": "运行中",
                "source": source,
                "started_at": self._now_text(),
                "finished_at": "-",
                "duration": "-",
                "target_path": str(target_dir),
                "media": display_name,
                "keyword": keyword or "-",
                "sites": 0,
                "total_subtitles": 0,
                "chinese_subtitles": 0,
                "site_errors": 0,
                "selected": "-",
                "downloaded_files": "-",
                "message": "开始处理",
                "error": "",
            })
        return started_at

    def _update_run(self, **kwargs):
        self._ensure_runtime_status()
        with self._status_lock:
            self._runtime.update(kwargs)

    def _finish_run(self, status: str, message: str, started_at: float, error: str = "", **kwargs):
        self._ensure_runtime_status()
        duration = max(time.monotonic() - started_at, 0)
        with self._status_lock:
            self._runtime.update(kwargs)
            self._runtime.update({
                "running": False,
                "status": status,
                "finished_at": self._now_text(),
                "duration": f"{duration:.1f}s",
                "message": message,
                "error": error,
            })
            record = {
                "finished_at": self._runtime.get("finished_at", "-"),
                "status": status,
                "source": self._runtime.get("source", "-"),
                "media": self._runtime.get("media", "-"),
                "target_path": self._runtime.get("target_path", "-"),
                "keyword": self._runtime.get("keyword", "-"),
                "message": message,
            }
            self._history.insert(0, record)
            self._history = self._history[:10]

    def _runtime_snapshot(self) -> Dict[str, Any]:
        self._ensure_runtime_status()
        with self._status_lock:
            snapshot = dict(self._runtime)
            snapshot["enabled"] = self._enabled if hasattr(self, "_enabled") else False
            snapshot["history"] = [dict(item) for item in self._history]
            return snapshot

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
        status = self._runtime_snapshot()
        alert_type = {
            "已下载": "success",
            "失败": "error",
            "下载失败": "warning",
            "未找到": "warning",
            "配置错误": "warning",
            "已跳过": "info",
            "运行中": "info",
        }.get(status.get("status"), "info")

        detail_rows = self._build_table_rows([
            ("插件开关", "已启用" if status.get("enabled") else "未启用"),
            ("运行状态", status.get("status", "-")),
            ("最近开始", status.get("started_at", "-")),
            ("最近结束", status.get("finished_at", "-")),
            ("耗时", status.get("duration", "-")),
            ("来源", status.get("source", "-")),
            ("媒体", status.get("media", "-")),
            ("目标目录", status.get("target_path", "-")),
            ("搜索关键词", status.get("keyword", "-")),
            ("站点数", status.get("sites", 0)),
            ("字幕总数", status.get("total_subtitles", 0)),
            ("中文字幕", status.get("chinese_subtitles", 0)),
            ("站点异常", status.get("site_errors", 0)),
            ("选择字幕", status.get("selected", "-")),
            ("下载文件", status.get("downloaded_files", "-")),
            ("错误", status.get("error", "")),
        ])

        history_rows = []
        for item in status.get("history", []):
            history_rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("finished_at", "-")},
                    {"component": "td", "text": item.get("status", "-")},
                    {"component": "td", "text": item.get("source", "-")},
                    {"component": "td", "text": item.get("media", "-")},
                    {"component": "td", "text": item.get("keyword", "-")},
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        if not history_rows:
            history_rows = [{
                "component": "tr",
                "content": [
                    {
                        "component": "td",
                        "props": {"colspan": 6},
                        "text": "暂无运行记录",
                    }
                ],
            }]

        return [
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
                                    "type": alert_type,
                                    "variant": "tonal",
                                    "text": f"{status.get('status', '未运行')}：{status.get('message', '')}",
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
                                "component": "VTable",
                                "props": {"hover": True},
                                "content": [
                                    {
                                        "component": "tbody",
                                        "content": detail_rows,
                                    }
                                ],
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
                                "component": "VTable",
                                "props": {"hover": True},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [{
                                            "component": "tr",
                                            "content": [
                                                {"component": "th", "text": "时间"},
                                                {"component": "th", "text": "状态"},
                                                {"component": "th", "text": "来源"},
                                                {"component": "th", "text": "媒体"},
                                                {"component": "th", "text": "关键词"},
                                                {"component": "th", "text": "结果"},
                                            ],
                                        }],
                                    },
                                    {
                                        "component": "tbody",
                                        "content": history_rows,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        ]

    @staticmethod
    def _build_table_rows(items: List[Tuple[str, Any]]) -> List[dict]:
        rows = []
        for label, value in items:
            if value is None or value == "":
                value = "-"
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": label},
                    {"component": "td", "text": str(value)},
                ],
            })
        return rows
