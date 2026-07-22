# _*_ coding: utf-8 _*_
"""SubtitleHunter plugin shell — config, service hooks, and mixin composition."""
from typing import Any, Dict, List, Optional

from apscheduler.triggers.cron import CronTrigger
from app.log import logger
from app.plugins import _PluginBase

from . import constants as _C
from .media_ops import MediaOpsMixin
from .models import SubtitleCue, SubtitleTrack  # noqa: F401 — package re-exports
from .runtime_ops import RuntimeOpsMixin
from .translate_ops import TranslateOpsMixin
from .ui import UIMixin
from .workflow import WorkflowMixin


class SubtitleHunter(
    _PluginBase,
    MediaOpsMixin,
    TranslateOpsMixin,
    WorkflowMixin,
    RuntimeOpsMixin,
    UIMixin,
):
    plugin_name = "SubtitleHunter"
    plugin_desc = "入库后自动检测、提取、翻译并规范化字幕"
    plugin_icon = "subtitle.png"
    plugin_version = "2.16"
    plugin_author = "milikii"
    author_url = "https://github.com/milikii"
    plugin_config_prefix = "subtitle_hunter_"
    plugin_order = 20
    auth_level = 1

    _VIDEO_EXTS = _C.VIDEO_EXTS
    _SUB_EXTS = _C.SUB_EXTS
    _TRANSLATABLE_EXTS = _C.TRANSLATABLE_EXTS
    _TEXT_CODECS = _C.TEXT_CODECS
    _IMAGE_CODECS = _C.IMAGE_CODECS
    _CHI_PATTERNS = _C.CHI_PATTERNS
    _ENG_PATTERNS = _C.ENG_PATTERNS
    _FORCED_PATTERNS = _C.FORCED_PATTERNS
    _SRT_TIME = _C.SRT_TIME
    _CACHE_MAX_FILES = _C.CACHE_MAX_FILES
    _CACHE_MAX_AGE_DAYS = _C.CACHE_MAX_AGE_DAYS

    def init_plugin(self, config: dict = None):
        config_changed = False
        self._enabled = False
        self._notify = True
        self._onlyonce = False
        self._target_path = ""
        self._schedule_cron = ""
        self._auto_ensure = True
        self._rename_existing = True
        self._extract_chinese_embedded = True
        self._overwrite = False
        self._keep_intermediate = True
        self._ai_enabled = False
        self._model_source = "system"
        self._api_base_url = "https://api.openai.com/v1"
        self._api_key = ""
        self._api_model = "gpt-4o-mini"
        self._api_use_proxy = False
        self._api_timeout = 180
        self._api_retries = 10
        self._batch_size = 60
        self._batch_chars = 9000
        self._parallel_batches = 5
        self._translation_profile = "quality"
        self._cache_enabled = True
        self._target_language = "zh-Hans"
        self._translation_suffix = "ai"
        self._glossary = ""
        self._enable_line_check = True
        self._ffmpeg_timeout = 600

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._target_path = config.get("target_path", "") or ""
            self._schedule_cron = config.get("schedule_cron", "") or ""
            self._auto_ensure = bool(config.get("auto_ensure", True))
            self._rename_existing = bool(config.get("rename_existing", True))
            self._extract_chinese_embedded = bool(config.get("extract_chinese_embedded", True))
            self._overwrite = bool(config.get("overwrite", False))
            self._keep_intermediate = bool(config.get("keep_intermediate", True))
            self._ai_enabled = bool(config.get("ai_enabled", False))
            self._model_source = config.get("model_source") or ("custom" if config.get("api_key") else "system")
            if self._model_source not in {"system", "custom"}:
                self._model_source = "system"
            self._api_base_url = config.get("api_base_url", self._api_base_url) or self._api_base_url
            self._api_key = config.get("api_key", "") or ""
            self._api_model = config.get("api_model", self._api_model) or self._api_model
            self._api_use_proxy = bool(config.get("api_use_proxy", False))
            self._api_timeout = self._safe_int(config.get("api_timeout"), 180, 15, 600)
            self._api_retries = self._safe_int(config.get("api_retries"), 10, 0, 100)
            if self._api_retries < 10:
                self._api_retries = 10
                config_changed = True
            self._batch_size = self._safe_int(config.get("batch_size"), 60, 5, 200)
            self._batch_chars = self._safe_int(config.get("batch_chars"), 9000, 1000, 30000)
            self._parallel_batches = self._safe_int(config.get("parallel_batches"), 5, 1, 20)
            self._translation_profile = config.get("translation_profile", self._translation_profile) or self._translation_profile
            if self._translation_profile not in {"fast", "standard", "quality"}:
                self._translation_profile = "quality"
            self._cache_enabled = bool(config.get("cache_enabled", True))
            self._target_language = config.get("target_language", self._target_language) or self._target_language
            self._translation_suffix = config.get("translation_suffix", self._translation_suffix) or self._translation_suffix
            self._glossary = config.get("glossary", "") or ""
            self._enable_line_check = bool(config.get("enable_line_check", True))
            self._ffmpeg_timeout = self._safe_int(config.get("ffmpeg_timeout"), 600, 60, 7200)

        self._init_runtime_status()
        try:
            self._load_runtime_state()
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】加载运行状态失败：{e}")

        if config_changed and not self._onlyonce:
            self.update_config(self._current_config())

        if self._onlyonce:
            self._onlyonce = False
            self.update_config(self._current_config(onlyonce=False))
            if self._split_target_paths(self._target_path):
                self._start_background_job(
                    source="手动运行",
                    target_path=self._target_path,
                    mediainfo=None,
                )
            else:
                message = "运行一次：未指定媒体目录或视频路径"
                self._update_run(status="配置错误", message=message, error=message)
                logger.warning(f"【{self.plugin_name}】{message}")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._schedule_cron:
            return [{
                "id": "SubtitleHunterSchedule",
                "name": "SubtitleHunter 定时处理",
                "trigger": CronTrigger.from_crontab(self._schedule_cron),
                "func": self._scheduled_run,
                "kwargs": {},
            }]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/status",
                "endpoint": self.get_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "获取 SubtitleHunter 运行状态",
            },
            {
                "path": "/list",
                "endpoint": self.api_list_subtitles,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "列出指定目录或视频的字幕",
            },
            {
                "path": "/ensure",
                "endpoint": self.api_ensure_chinese,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "异步确保指定目录或视频有中文字幕",
            },
            {
                "path": "/extract",
                "endpoint": self.api_extract_subtitles,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "提取指定视频的内嵌文本字幕",
            },
            {
                "path": "/test",
                "endpoint": self.api_test_ai,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "测试 AI 翻译配置连通性",
            },
            {
                "path": "/cancel",
                "endpoint": self.api_cancel,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "请求取消当前字幕任务",
            },
        ]

    def get_module(self) -> Dict[str, Any]:
        return {}

    @staticmethod
    def _safe_int(value: Any, default: int, min_value: int = 0, max_value: int = 999999) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(min_value, min(number, max_value))

    def _current_config(self, onlyonce: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
            "target_path": self._target_path,
            "schedule_cron": self._schedule_cron,
            "auto_ensure": self._auto_ensure,
            "rename_existing": self._rename_existing,
            "extract_chinese_embedded": self._extract_chinese_embedded,
            "overwrite": self._overwrite,
            "keep_intermediate": self._keep_intermediate,
            "ai_enabled": self._ai_enabled,
            "model_source": self._model_source,
            "api_base_url": self._api_base_url,
            "api_key": self._api_key,
            "api_model": self._api_model,
            "api_use_proxy": self._api_use_proxy,
            "api_timeout": self._api_timeout,
            "api_retries": self._api_retries,
            "batch_size": self._batch_size,
            "batch_chars": self._batch_chars,
            "parallel_batches": self._parallel_batches,
            "translation_profile": self._translation_profile,
            "cache_enabled": self._cache_enabled,
            "target_language": self._target_language,
            "translation_suffix": self._translation_suffix,
            "glossary": self._glossary,
            "enable_line_check": self._enable_line_check,
            "ffmpeg_timeout": self._ffmpeg_timeout,
        }
