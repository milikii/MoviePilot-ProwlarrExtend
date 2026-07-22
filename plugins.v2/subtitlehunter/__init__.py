# _*_ coding: utf-8 _*_
import hashlib
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
import requests
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .codes import Stage
from . import constants as _C
from . import eta as eta_mod
from . import formats as formats_mod
from . import language as language_mod
from . import quality as quality_mod
from .models import SubtitleCue, SubtitleTrack
from .media_ops import MediaOpsMixin
from .runtime_ops import RuntimeOpsMixin
from .ui import UIMixin

class SubtitleHunter(_PluginBase, MediaOpsMixin, RuntimeOpsMixin, UIMixin):
    plugin_name = "SubtitleHunter"
    plugin_desc = "入库后自动检测、提取、翻译并规范化字幕"
    plugin_icon = "subtitle.png"
    plugin_version = "2.15"
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

    def _scheduled_run(self):
        """Run the configured scheduled subtitle workflow when no job is active."""
        self._ensure_runtime_status()
        with self._status_lock:
            runtime = dict(self._runtime)
        if runtime.get("running"):
            message = "上一次任务仍在运行，已跳过本次"
            logger.warning(f"【{self.plugin_name}】定时任务跳过：{message}")
            self._send_notify("定时任务跳过", self._build_skip_notify_text(message))
            return

        if not self._target_path:
            message = "未配置媒体目录或视频路径"
            logger.warning(f"【{self.plugin_name}】定时任务跳过：{message}")
            self._send_notify("定时任务跳过", self._build_skip_notify_text(message))
            return
        if not self._split_target_paths(self._target_path):
            message = "媒体目录或视频路径为空"
            logger.warning(f"【{self.plugin_name}】定时任务跳过：{message}")
            self._send_notify("定时任务跳过", self._build_skip_notify_text(message))
            return

        self._start_background_job(
            source="定时任务",
            target_path=self._target_path,
            mediainfo=None,
        )

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

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled or not self._auto_ensure:
            return

        event_data = event.event_data or {}
        mediainfo: MediaInfo = event_data.get("mediainfo")
        transferinfo = event_data.get("transferinfo")

        if not transferinfo:
            logger.warning(f"【{self.plugin_name}】入库完成事件缺少 transferinfo，跳过")
            return

        target_path = getattr(transferinfo, "target_path", None)
        if not target_path:
            logger.warning(f"【{self.plugin_name}】入库完成事件缺少目标路径，跳过")
            return

        title = self._media_title(mediainfo, Path(target_path))
        logger.info(f"【{self.plugin_name}】收到入库完成事件：{title}，目标：{target_path}")
        self._start_background_job(
            source="入库事件",
            target_path=str(target_path),
            mediainfo=mediainfo,
        )

    def api_list_subtitles(self, path: str = "") -> Dict[str, Any]:
        target_paths = self._split_target_paths(path or self._target_path)
        if not target_paths:
            return {"success": False, "message": "未指定 path"}

        targets = [self._resolve_target_path(item) for item in target_paths]
        result = {"videos": [], "subtitles": [], "errors": []}
        for target in targets:
            scan = self._scan_target(target)
            result["videos"].extend(scan["videos"])
            result["subtitles"].extend(scan["subtitles"])
            result["errors"].extend(scan["errors"])
        return {
            "success": True,
            "target": ",".join(str(target) for target in targets),
            "targets": [str(target) for target in targets],
            "videos": [str(video) for video in result["videos"]],
            "subtitles": [track.to_dict() for track in result["subtitles"]],
            "errors": result["errors"],
        }

    def api_ensure_chinese(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        target_path = payload.get("path") or self._target_path
        target_paths = self._split_target_paths(target_path)
        if not target_paths:
            return {"success": False, "message": "未指定 path"}
        started = self._start_background_job(
            source="API确保中文字幕",
            target_path=target_path,
            mediainfo=None,
        )
        if not started:
            return {"success": False, "message": "已有字幕任务正在运行", "target": target_path}
        return {"success": True, "message": "任务已提交", "target": target_path, "targets": target_paths}

    def api_extract_subtitles(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        video_path = self._resolve_target_path(payload.get("path") or "")
        if not video_path or not video_path.is_file() or video_path.suffix.lower() not in self._VIDEO_EXTS:
            return {"success": False, "message": "path 必须是视频文件"}

        stream_index = payload.get("stream_index")
        tracks = self._probe_embedded_subtitles(video_path)
        if stream_index is not None:
            tracks = [track for track in tracks if track.stream_index == self._safe_int(stream_index, -1, -1, 9999)]

        extracted = []
        skipped = []
        for track in tracks:
            if not track.text_based:
                skipped.append({**track.to_dict(), "reason": "图形字幕无法直接提取为 srt/ass"})
                continue
            ok, output, message = self._extract_embedded_subtitle(track)
            if ok:
                extracted.append(str(output))
            else:
                skipped.append({**track.to_dict(), "reason": message})

        return {
            "success": True,
            "video": str(video_path),
            "extracted": extracted,
            "skipped": skipped,
        }

    def api_test_ai(self) -> Dict[str, Any]:
        """Validate the active LLM config with a tiny chat completion call."""
        ai_config, ai_error = self._resolve_ai_config()
        if not ai_config:
            return {"success": False, "message": ai_error or "AI 翻译不可用"}
        try:
            content = self._chat_completion([
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "ping"},
            ], ai_config)
            preview = re.sub(r"\s+", " ", str(content or "")).strip()[:120]
            return {
                "success": True,
                "message": "AI 连接成功",
                "source": ai_config.get("source"),
                "model": ai_config.get("model"),
                "base_url": ai_config.get("base_url"),
                "preview": preview,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"AI 连接失败：{e}",
                "source": ai_config.get("source"),
                "model": ai_config.get("model"),
                "base_url": ai_config.get("base_url"),
            }

    def _ensure_multiple_targets_workflow(
        self,
        source: str,
        targets: List[Path],
    ):
        total = len(targets)
        logger.info(f"【{self.plugin_name}】开始多目标字幕任务：{total} 个目标")
        display_name = f"{total} 个目标"
        started_at = self._start_run(source=source, target=targets[0], display_name=display_name)
        aggregate = {
            "processed": 0,
            "skipped": 0,
            "extracted": 0,
            "translated": 0,
            "renamed": 0,
            "failed": 0,
        }
        details: List[Dict[str, Any]] = []
        try:
            for index, target in enumerate(targets, start=1):
                self._raise_if_cancelled()
                title = self._media_title(None, target)
                scoped_source = f"{source} {index}/{total}"
                self._update_run(
                    message=f"多目标进度：{index}/{total} · {title}",
                    media=f"{display_name}（当前 {title}）",
                    target_path=str(target),
                )
                self._ensure_chinese_workflow(
                    scoped_source,
                    target,
                    title,
                    None,
                    aggregate_into=aggregate,
                    aggregate_details=details,
                    outer_started_at=started_at,
                )
            final_status = "完成" if aggregate["failed"] == 0 else "部分失败"
            if self._is_cancelled():
                final_status = "已取消"
            message = (
                f"多目标处理完成：目标 {total}，视频 {aggregate['processed']}，"
                f"跳过 {aggregate['skipped']}，提取 {aggregate['extracted']}，"
                f"翻译 {aggregate['translated']}，重命名 {aggregate['renamed']}，"
                f"失败 {aggregate['failed']}"
            )
            self._finish_run(final_status, message, started_at, details=details[-10:], **aggregate)
            self._send_notify(
                final_status,
                self._build_finish_notify_text(
                    final_status=final_status,
                    source=source,
                    display_name=display_name,
                    target=targets[0],
                    summary=aggregate,
                    details=details,
                    duration_seconds=max(time.monotonic() - started_at, 0),
                ),
            )
        except self._JobCancelled:
            message = f"多目标任务已取消：已完成部分 {aggregate['processed']} 个视频"
            self._finish_run("已取消", message, started_at, details=details[-10:], **aggregate)
            self._send_notify("已取消", self._build_skip_notify_text(message, source=source))

    def _ensure_chinese_workflow(
        self,
        source: str,
        target: Path,
        display_name: str,
        mediainfo: Optional[MediaInfo],
        aggregate_into: Optional[Dict[str, int]] = None,
        aggregate_details: Optional[List[Dict[str, Any]]] = None,
        outer_started_at: Optional[float] = None,
    ):
        nested = aggregate_into is not None
        started_at = outer_started_at if nested and outer_started_at is not None else self._start_run(
            source=source, target=target, display_name=display_name
        )
        if nested:
            self._update_run(source=source, target_path=str(target), media=display_name)
        try:
            self._raise_if_cancelled()
            if not target.exists():
                message = f"目标不存在：{target}"
                logger.error(f"【{self.plugin_name}】{message}")
                if not nested:
                    self._finish_run("失败", message, started_at, error=message)
                elif aggregate_into is not None:
                    aggregate_into["failed"] += 1
                return

            media_context = self._build_media_context(mediainfo, target)
            scan = self._scan_target(target)
            videos = scan["videos"]
            if not videos:
                message = f"未发现视频文件：{target}"
                logger.warning(f"【{self.plugin_name}】{message}")
                if not nested:
                    self._finish_run("已跳过", message, started_at, errors=scan["errors"])
                return

            self._update_run(
                videos=len(videos) if not nested else self._runtime.get("videos", 0) + len(videos),
                subtitles=len(scan["subtitles"]) if not nested else self._runtime.get("subtitles", 0) + len(scan["subtitles"]),
                message=f"发现 {len(videos)} 个视频，开始处理字幕",
                errors=scan["errors"],
            )
            eta_seconds = self._estimate_eta_seconds(
                len(videos),
                self._estimate_scan_subtitle_chars(scan["subtitles"]),
            )
            if not nested:
                notify_text = self._build_start_notify_text(
                    source=source,
                    display_name=display_name,
                    target=target,
                    video_count=len(videos),
                    subtitle_count=len(scan["subtitles"]),
                    eta_seconds=eta_seconds,
                    scan_errors=scan["errors"],
                )
                self._send_notify("开始处理字幕", notify_text)

            summary = {
                "processed": 0,
                "skipped": 0,
                "extracted": 0,
                "translated": 0,
                "renamed": 0,
                "failed": 0,
            }
            details = []

            for video in videos:
                self._raise_if_cancelled()
                detail = self._ensure_video_chinese(video, media_context)
                details.append(detail)
                summary["processed"] += 1
                summary["skipped"] += 1 if detail["status"] in {"已有中文", "已跳过"} else 0
                summary["extracted"] += len(detail.get("extracted_files", []))
                summary["translated"] += len(detail.get("translated_files", []))
                summary["renamed"] += len(detail.get("renamed_files", []))
                summary["failed"] += 1 if detail["status"] == "失败" else 0
                progress = dict(summary)
                if aggregate_into is not None:
                    for key in aggregate_into:
                        progress[key] = aggregate_into[key] + summary[key]
                self._update_run(
                    processed=progress["processed"],
                    skipped=progress["skipped"],
                    extracted=progress["extracted"],
                    translated=progress["translated"],
                    renamed=progress["renamed"],
                    failed=progress["failed"],
                    last_video=str(video),
                    details=(aggregate_details or details)[-10:] if aggregate_details is not None else details[-10:],
                    message=f"处理中：{summary['processed']}/{len(videos)}",
                )

            if aggregate_into is not None:
                for key, value in summary.items():
                    aggregate_into[key] = aggregate_into.get(key, 0) + value
            if aggregate_details is not None:
                aggregate_details.extend(details)

            final_status = "完成" if summary["failed"] == 0 else "部分失败"
            message = (
                f"处理完成：视频 {summary['processed']}，跳过 {summary['skipped']}，"
                f"提取 {summary['extracted']}，翻译 {summary['translated']}，"
                f"重命名 {summary['renamed']}，失败 {summary['failed']}"
            )
            if nested:
                return

            duration_seconds = max(time.monotonic() - started_at, 0)
            notify_text = self._build_finish_notify_text(
                final_status=final_status,
                source=source,
                display_name=display_name,
                target=target,
                summary=summary,
                details=details,
                duration_seconds=duration_seconds,
            )
            self._finish_run(final_status, message, started_at, details=details, **summary)
            self._send_notify(final_status, notify_text)

        except self._JobCancelled:
            if nested:
                raise
            message = f"{display_name} 字幕处理已取消"
            self._finish_run("已取消", message, started_at)
            self._send_notify("已取消", self._build_skip_notify_text(message, source=source))
        except Exception as e:
            message = f"{display_name} 字幕处理失败：{e}"
            logger.error(f"【{self.plugin_name}】{message}\n{traceback.format_exc()}")
            if nested and aggregate_into is not None:
                aggregate_into["failed"] = aggregate_into.get("failed", 0) + 1
                return
            self._finish_run("失败", message, started_at, error=traceback.format_exc())
            self._send_notify(
                "字幕处理失败",
                self._build_failure_notify_text(source, display_name, target, e),
            )

    def _ensure_video_chinese(self, video_path: Path, media_context: str) -> Dict[str, Any]:
        detail = {
            "video": str(video_path),
            "status": "处理中",
            "message": "",
            "extracted_files": [],
            "translated_files": [],
            "renamed_files": [],
            "errors": [],
        }

        try:
            external_tracks = self._find_external_subtitles(video_path)
            embedded_tracks = self._probe_embedded_subtitles(video_path)
            all_tracks = external_tracks + embedded_tracks

            if self._rename_existing:
                renamed = self._rename_external_subtitles(video_path, external_tracks)
                detail["renamed_files"].extend(renamed)
                if renamed:
                    external_tracks = self._find_external_subtitles(video_path)
                    all_tracks = external_tracks + embedded_tracks

            chinese_tracks = [
                track for track in all_tracks
                if not track.forced
                and self._is_chinese_language(track.language, track.title, track.path)
            ]
            external_chinese = [
                track for track in chinese_tracks
                if track.source == "external" and self._is_usable_external_chinese_track(track)
            ]
            if external_chinese:
                detail["status"] = "已有中文"
                detail["message"] = f"已有外挂中文字幕：{external_chinese[0].path}"
                return detail

            embedded_chinese = [track for track in chinese_tracks if track.source == "embedded"]
            if embedded_chinese and self._extract_chinese_embedded:
                for track in embedded_chinese:
                    if not track.text_based:
                        detail["errors"].append(f"内嵌中文字幕是图形字幕，无法直接提取为文本：stream {track.stream_index}")
                        continue
                    ok, output, message = self._extract_embedded_subtitle(track)
                    if ok:
                        detail["extracted_files"].append(str(output))
                if detail["extracted_files"]:
                    detail["status"] = "已有中文"
                    detail["message"] = "已提取内嵌中文字幕为外挂字幕"
                    return detail

            english_source = self._select_english_source(video_path, external_tracks, embedded_tracks, detail)
            if not english_source:
                detail["status"] = "失败"
                detail["message"] = "未找到可翻译的英文文本字幕"
                return detail

            ai_config, ai_error = self._resolve_ai_config()
            if not ai_config:
                detail["status"] = "已跳过"
                detail["message"] = f"没有中文字幕，但 AI 翻译不可用：{ai_error}"
                return detail

            translated_path = self._translated_subtitle_path(video_path, english_source)
            if (
                translated_path.exists()
                and not self._overwrite
                and self._subtitle_file_has_chinese_text(translated_path)
            ):
                detail["status"] = "已有中文"
                detail["message"] = f"翻译字幕已存在：{translated_path}"
                return detail

            ok, message = self._translate_subtitle_file(
                source_path=english_source,
                output_path=translated_path,
                media_context=media_context,
                ai_config=ai_config,
            )
            if ok:
                detail["translated_files"].append(str(translated_path))
                detail["status"] = "已生成中文"
                detail["message"] = message
            else:
                detail["status"] = "失败"
                detail["message"] = message

            return detail

        except Exception as e:
            detail["status"] = "失败"
            detail["message"] = str(e)
            detail["errors"].append(traceback.format_exc())
            logger.error(f"【{self.plugin_name}】处理视频失败：{video_path}，{e}\n{traceback.format_exc()}")
            return detail

    def _select_english_source(
        self,
        video_path: Path,
        external_tracks: List[SubtitleTrack],
        embedded_tracks: List[SubtitleTrack],
        detail: Dict[str, Any],
    ) -> Optional[Path]:
        external_english = [
            track for track in external_tracks
            if track.path and track.path.suffix.lower() in self._TRANSLATABLE_EXTS
            and self._is_english_language(track.language, track.title, track.path)
        ]
        if external_english:
            return external_english[0].path

        embedded_english = [
            track for track in embedded_tracks
            if track.text_based and self._is_english_language(track.language, track.title, track.path)
        ]
        if not embedded_english:
            embedded_english = [
                track for track in embedded_tracks
                if track.text_based and not self._is_chinese_language(track.language, track.title, track.path)
            ]

        for track in embedded_english:
            ok, output, message = self._extract_embedded_subtitle(track)
            if ok and output:
                detail["extracted_files"].append(str(output))
                return output
            detail["errors"].append(message)

        image_english = [
            track for track in embedded_tracks
            if not track.text_based and self._is_english_language(track.language, track.title, track.path)
        ]
        if image_english:
            detail["errors"].append("找到英文图形字幕，但当前版本不做 OCR，无法翻译")

        return None

    def _translate_subtitle_file(
        self,
        source_path: Path,
        output_path: Path,
        media_context: str,
        ai_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        suffix = source_path.suffix.lower()
        temp_path: Optional[Path] = None
        try:
            content = source_path.read_text(encoding="utf-8-sig", errors="ignore")
            if suffix == ".srt":
                cues = self._parse_srt(content)
                renderer = self._render_srt
            elif suffix in {".ass", ".ssa"}:
                lines, cues = self._parse_ass(content)

                def renderer(items, _lines=lines):
                    return self._render_ass(_lines, items)
            else:
                return False, f"暂不支持翻译 {suffix} 字幕"

            if not cues:
                return False, "字幕翻译失败：未解析到有效字幕条目"

            translated = self._translate_cues(cues, media_context, ai_config)
            valid, reason = self._validate_translated_cues(cues, translated)
            if not valid:
                return False, f"字幕翻译失败：{reason}"

            rendered = renderer(translated)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = output_path.with_name(
                f".{output_path.name}.{threading.get_ident()}.tmp"
            )
            temp_path.write_text(rendered, encoding="utf-8")
            os.replace(temp_path, output_path)
            temp_path = None
            logger.info(f"【{self.plugin_name}】字幕翻译完成：{source_path} -> {output_path}")
            return True, f"翻译完成：{output_path}"
        except Exception as e:
            logger.error(f"【{self.plugin_name}】字幕翻译失败：{source_path}，{e}\n{traceback.format_exc()}")
            return False, f"字幕翻译失败：{e}"
        finally:
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _validate_translated_cues(
        self,
        source: List[SubtitleCue],
        translated: List[SubtitleCue],
    ) -> Tuple[bool, str]:
        return quality_mod.validate_translated_cues(source, translated)

    def _has_chinese_cue_coverage(self, cues: List[SubtitleCue]) -> bool:
        return quality_mod.has_chinese_cue_coverage(cues)

    def _translate_cues(
        self,
        cues: List[SubtitleCue],
        media_context: str,
        ai_config: Dict[str, Any],
    ) -> List[SubtitleCue]:
        translated_by_index: Dict[int, str] = {}
        translatable = [cue for cue in cues if self._plain_subtitle_text(cue.text).strip()]

        batches = self._chunk_cues(translatable)
        if not batches:
            return cues

        stage_started = self._stage_begin(Stage.TRANSLATE)
        try:
            ai_glossary = self._generate_ai_glossary(batches, media_context, ai_config) if self._ai_enabled else {}
            glossary_text = self._build_effective_glossary(ai_glossary)
            workers = min(max(self._parallel_batches, 1), len(batches))
            logger.info(
                f"【{self.plugin_name}】开始翻译：{len(translatable)} 条字幕，"
                f"{len(batches)} 个批次，并发 {workers}，模式 {self._translation_profile}"
            )

            completed = 0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="SubtitleHunterTranslate") as executor:
                futures = {
                    executor.submit(
                        self._translate_batch,
                        batch_no,
                        len(batches),
                        batch,
                        media_context,
                        ai_config,
                        glossary_text,
                    ): (batch_no, batch)
                    for batch_no, batch in enumerate(batches, start=1)
                }
                try:
                    for future in as_completed(futures):
                        self._raise_if_cancelled()
                        batch_no, batch = futures[future]
                        result = future.result()
                        for cue in batch:
                            translated_by_index[cue.index] = result.get(cue.index) or cue.text
                        completed += 1
                        self._update_run(
                            message=f"翻译批次完成：{completed}/{len(batches)}",
                            translation_batches_total=len(batches),
                            translation_batches_done=completed,
                        )
                        logger.info(
                            f"【{self.plugin_name}】翻译批次完成 {completed}/{len(batches)}："
                            f"batch {batch_no}"
                        )
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise

            output = []
            for cue in cues:
                if cue.index in translated_by_index:
                    output.append(SubtitleCue(
                        index=cue.index,
                        start=cue.start,
                        end=cue.end,
                        text=translated_by_index[cue.index],
                        line_index=cue.line_index,
                        ass_fields=list(cue.ass_fields) if cue.ass_fields else None,
                        ass_text_index=cue.ass_text_index,
                    ))
                else:
                    output.append(cue)
            return self._validate_line_length(output, media_context, ai_config, glossary_text)
        finally:
            self._stage_end(Stage.TRANSLATE, stage_started)

    def _translate_batch(
        self,
        batch_no: int,
        total_batches: int,
        batch: List[SubtitleCue],
        media_context: str,
        ai_config: Dict[str, Any],
        glossary_text: str,
    ) -> Dict[int, str]:
        logger.info(
            f"【{self.plugin_name}】翻译批次 {batch_no}/{total_batches}："
            f"{len(batch)} 条字幕，模式 {self._translation_profile}"
        )
        source_items = [
            {"index": cue.index, "text": self._plain_subtitle_text(cue.text)}
            for cue in batch
        ]

        if self._translation_profile == "fast":
            return self._translate_stage(
                stage="direct",
                items=source_items,
                media_context=media_context,
                previous=None,
                ai_config=ai_config,
                glossary_text=glossary_text,
            )

        literal = self._translate_stage(
            stage="literal",
            items=source_items,
            media_context=media_context,
            previous=None,
            ai_config=ai_config,
            glossary_text=glossary_text,
        )

        if self._translation_profile == "standard":
            polished = self._translate_stage(
                stage="polish",
                items=source_items,
                media_context=media_context,
                previous=literal,
                ai_config=ai_config,
                glossary_text=glossary_text,
            )
            return {
                cue.index: polished.get(cue.index) or literal.get(cue.index) or cue.text
                for cue in batch
            }

        reflected = self._translate_stage(
            stage="reflect",
            items=source_items,
            media_context=media_context,
            previous=literal,
            ai_config=ai_config,
            glossary_text=glossary_text,
        )
        final = self._translate_stage(
            stage="polish",
            items=source_items,
            media_context=media_context,
            previous=reflected,
            ai_config=ai_config,
            glossary_text=glossary_text,
        )
        return {
            cue.index: final.get(cue.index) or reflected.get(cue.index) or literal.get(cue.index) or cue.text
            for cue in batch
        }

    def _translate_stage(
        self,
        stage: str,
        items: List[Dict[str, Any]],
        media_context: str,
        previous: Optional[Dict[int, str]],
        ai_config: Dict[str, Any],
        glossary_text: Optional[str] = None,
    ) -> Dict[int, str]:
        stage_name = {
            "direct": "快速翻译",
            "literal": "第一遍直译",
            "reflect": "第二遍反思修正",
            "polish": "第三遍意译润色",
            "glossary_gen": "术语抽取",
            "compress": "字幕压缩",
        }.get(stage, stage)

        cache_key = self._translation_cache_key(stage, items, media_context, previous, ai_config, glossary_text)
        cached = self._load_translation_cache(cache_key)
        if cached:
            logger.info(f"【{self.plugin_name}】{stage_name}命中缓存：{len(cached)} 条")
            return cached

        payload = {
            "items": items,
            "previous": [
                {"index": index, "text": text}
                for index, text in sorted((previous or {}).items())
            ],
        }
        prompt = self._translation_prompt(stage, media_context, glossary_text)
        requested_indexes = {int(item["index"]) for item in items}
        attempts = self._api_retries + 1
        last_error = None

        for attempt in range(attempts):
            try:
                content = self._chat_completion([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ], ai_config)
                parsed = self._parse_translation_response(content)
                if not parsed:
                    raise RuntimeError(f"{stage_name}返回为空：{content[:200]}")
                missing = requested_indexes - set(parsed.keys())
                if missing and stage != "compress":
                    raise RuntimeError(f"{stage_name}返回缺少字幕索引：{sorted(missing)[:10]}")
                if missing:
                    logger.warning(f"【{self.plugin_name}】{stage_name}返回缺少字幕索引，缺失条目保留原文：{sorted(missing)[:10]}")
                self._save_translation_cache(cache_key, parsed, ai_config)
                logger.info(f"【{self.plugin_name}】{stage_name}完成：{len(parsed)} 条")
                return parsed
            except MemoryError:
                raise
            except Exception as e:
                last_error = e
                if attempt >= attempts - 1:
                    break
                delay = max(1, self._api_timeout // 60) * (attempt + 1)
                logger.warning(
                    f"【{self.plugin_name}】{stage_name}失败，"
                    f"{delay}s 后重试 {attempt + 1}/{self._api_retries}：{e}"
                )
                self._interruptible_sleep(delay)

        raise RuntimeError(f"{stage_name}失败，已重试 {self._api_retries} 次：{last_error}")

    def _translation_cache_key(
        self,
        stage: str,
        items: List[Dict[str, Any]],
        media_context: str,
        previous: Optional[Dict[int, str]],
        ai_config: Dict[str, Any],
        glossary_text: Optional[str] = None,
    ) -> str:
        payload = {
            "version": 5,
            "stage": stage,
            "source": ai_config.get("source"),
            "base_url": ai_config.get("base_url"),
            "model": ai_config.get("model"),
            "target_language": self._target_language,
            "translation_profile": self._translation_profile,
            "glossary": self._glossary if glossary_text is None else glossary_text,
            "media_context": media_context,
            "items": items,
            "previous": [
                {"index": index, "text": text}
                for index, text in sorted((previous or {}).items())
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _translation_cache_path(self, cache_key: str) -> Path:
        return self.get_data_path() / "translation_cache" / f"{cache_key}.json"

    def _load_translation_cache(self, cache_key: str) -> Optional[Dict[int, str]]:
        if not self._cache_enabled:
            return None
        path = self._translation_cache_path(cache_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = data.get("result") or []
            return {
                int(item.get("index")): str(item.get("text") or "")
                for item in result
            }
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】读取翻译缓存失败：{path}，{e}")
            return None

    def _save_translation_cache(self, cache_key: str, result: Dict[int, str], ai_config: Dict[str, Any]):
        if not self._cache_enabled:
            return
        path = self._translation_cache_path(cache_key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "created_at": self._now_text(),
                "source": ai_config.get("source"),
                "model": ai_config.get("model"),
                "target_language": self._target_language,
                "translation_profile": self._translation_profile,
                "result": [
                    {"index": index, "text": text}
                    for index, text in sorted(result.items())
                ],
            }
            tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(path)
            self._cleanup_translation_cache()
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】写入翻译缓存失败：{path}，{e}")

    def _cleanup_translation_cache(self):
        """Drop oldest translation cache files when count or age exceeds limits."""
        cache_dir = self.get_data_path() / "translation_cache"
        if not cache_dir.exists():
            return
        try:
            files = [path for path in cache_dir.glob("*.json") if path.is_file()]
        except OSError:
            return
        if not files:
            return

        now = time.time()
        max_age = self._CACHE_MAX_AGE_DAYS * 86400
        kept = []
        removed = 0
        for path in files:
            try:
                age = now - path.stat().st_mtime
                if age > max_age:
                    path.unlink(missing_ok=True)
                    removed += 1
                else:
                    kept.append(path)
            except OSError:
                continue

        if len(kept) > self._CACHE_MAX_FILES:
            kept.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0)
            for path in kept[: max(0, len(kept) - self._CACHE_MAX_FILES)]:
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
        if removed:
            logger.info(f"【{self.plugin_name}】已清理翻译缓存 {removed} 个文件")

    def _translation_prompt(self, stage: str, media_context: str, glossary_text: Optional[str] = None) -> str:
        glossary = (self._glossary if glossary_text is None else glossary_text).strip() or "无"
        if stage == "glossary_gen":
            return self._glossary_prompt(media_context, glossary)
        if stage == "compress":
            return self._compress_prompt(media_context, glossary)
        base = (
            "你是专业影视字幕译者。你必须只返回 JSON 数组，不要 Markdown，不要解释。"
            "数组元素格式为 {\"index\": 数字, \"text\": \"译文\"}。"
            "必须保留 index，不要合并、拆分、增删条目。"
            "译文使用简体中文，适合 Jellyfin/Emby/Plex 字幕播放。"
            "每条字幕尽量不超过两行；避免机器腔；保留必要专有名词。"
            "英文字幕常把一个句子拆成相邻多条，你必须结合同批次前后文理解，"
            "可在相邻条目之间调整中文语序，让每条单独显示时自然，但不能改变 index 数量。"
            "例如连续两条是“You were the only person who was nice to me”与“when I moved here.”，"
            "不要译成“你是唯一对我好的人 / 在我刚搬来的时候”，应改成“我刚搬来这里时 / 只有你对我好”。"
            "不要输出时间轴，不要输出原文。"
            f"\n影片上下文：{media_context or '未知'}"
            f"\n术语表：{glossary}"
        )
        if stage == "direct":
            return base + "\n任务：快速翻译。一次完成准确翻译和自然润色，优先保证信息完整、语序自然、字幕可读。"
        if stage == "literal":
            return base + "\n任务：第一遍直译，准确覆盖每个英文字幕的信息，不做过度润色。"
        if stage == "reflect":
            return base + "\n任务：第二遍反思修正。根据原文和 previous 修正误译、漏译、术语不一致、代词指代错误。"
        return base + "\n任务：第三遍意译润色。根据原文和 previous 输出自然、克制、有影视字幕感的最终译文。"

    def _chat_completion(self, messages: List[Dict[str, str]], ai_config: Dict[str, Any]) -> str:
        base_url = (ai_config.get("base_url") or "").rstrip("/")
        if base_url.endswith("/chat/completions"):
            url = base_url
        else:
            url = f"{base_url}/chat/completions"

        body = {
            "model": ai_config.get("model"),
            "messages": messages,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {ai_config.get('api_key')}",
            "Content-Type": "application/json",
        }
        if ai_config.get("user_agent"):
            headers["User-Agent"] = ai_config["user_agent"]
        try:
            proxies = getattr(settings, "PROXY", None) if ai_config.get("use_proxy") else None
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=self._api_timeout,
                proxies=proxies,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"AI API HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
        except Exception as e:
            raise RuntimeError(f"AI API 请求失败：{e}") from e

        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"AI API 返回格式不兼容：{payload}") from e

    def _parse_translation_response(self, content: str) -> Dict[int, str]:
        return quality_mod.parse_translation_response(content)

    def _chunk_cues(self, cues: List[SubtitleCue]) -> List[List[SubtitleCue]]:
        chunks = []
        lookahead = 5
        total = len(cues)
        start = 0
        while start < total:
            end = start
            chars = 0
            soft_count = max(1, int(self._batch_size * 0.9))
            soft_chars = max(1, int(self._batch_chars * 0.9))
            while end < total:
                text_len = len(cues[end].text)
                if end > start and (end - start >= self._batch_size or chars + text_len > self._batch_chars):
                    break
                chars += text_len
                end += 1
                if end >= total or end - start >= soft_count or chars >= soft_chars:
                    break

            if end >= total:
                chunks.append(cues[start:end])
                break

            cut = end if self._is_sentence_boundary(cues[end - 1].text) else 0
            probe_end = end
            probe_chars = chars
            while not cut and probe_end < total and probe_end - end < lookahead:
                text_len = len(cues[probe_end].text)
                if probe_end - start >= self._batch_size or probe_chars + text_len > self._batch_chars:
                    break
                probe_chars += text_len
                probe_end += 1
                if self._is_sentence_boundary(cues[probe_end - 1].text):
                    cut = probe_end
                    break

            if not cut:
                hard_end = end
                hard_chars = chars
                while hard_end < total:
                    text_len = len(cues[hard_end].text)
                    if hard_end - start >= self._batch_size or hard_chars + text_len > self._batch_chars:
                        break
                    hard_chars += text_len
                    hard_end += 1
                cut = hard_end

            chunks.append(cues[start:cut])
            start = cut
        return chunks

    def _generate_ai_glossary(
        self,
        batches: List[List[SubtitleCue]],
        media_context: str,
        ai_config: Dict[str, Any],
    ) -> Dict[str, str]:
        """Extract and merge a movie-wide AI glossary from all subtitle batches."""
        glossary: Dict[str, str] = {}
        seen = set()
        logger.info(f"【{self.plugin_name}】开始生成 AI 术语表：{len(batches)} 个批次")
        for batch_no, batch in enumerate(batches, start=1):
            self._raise_if_cancelled()
            try:
                terms = self._run_glossary_stage(batch_no, len(batches), batch, media_context, ai_config)
                for term, translation in terms.items():
                    key = self._glossary_key(term)
                    if not key or key in seen:
                        continue
                    glossary[term.strip()] = translation.strip()
                    seen.add(key)
            except Exception as e:
                logger.warning(
                    f"【{self.plugin_name}】术语抽取批次 {batch_no}/{len(batches)} 失败，"
                    f"跳过该批次继续翻译：{e}"
                )
        logger.info(f"【{self.plugin_name}】AI 术语表生成完成：{len(glossary)} 条")
        return glossary

    def _run_glossary_stage(
        self,
        batch_no: int,
        total_batches: int,
        batch: List[SubtitleCue],
        media_context: str,
        ai_config: Dict[str, Any],
    ) -> Dict[str, str]:
        """Run the cached glossary_gen stage for one subtitle batch."""
        items = [
            {"index": cue.index, "text": self._plain_subtitle_text(cue.text)}
            for cue in batch
        ]
        cache_key = self._translation_cache_key(
            "glossary_gen",
            items,
            media_context,
            previous=None,
            ai_config=ai_config,
            glossary_text=self._glossary,
        )
        cached = self._load_glossary_cache(cache_key)
        if cached is not None:
            logger.info(f"【{self.plugin_name}】术语抽取命中缓存：batch {batch_no}/{total_batches}，{len(cached)} 条")
            return cached

        payload = {"items": items}
        prompt = self._translation_prompt("glossary_gen", media_context, self._glossary)
        attempts = self._api_retries + 1
        last_error = None
        for attempt in range(attempts):
            try:
                content = self._chat_completion([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ], ai_config)
                parsed = self._parse_glossary_response(content)
                self._save_glossary_cache(cache_key, parsed, ai_config)
                logger.info(
                    f"【{self.plugin_name}】术语抽取完成：batch {batch_no}/{total_batches}，"
                    f"{len(parsed)} 条"
                )
                return parsed
            except MemoryError:
                raise
            except Exception as e:
                last_error = e
                if attempt >= attempts - 1:
                    break
                delay = max(1, self._api_timeout // 60) * (attempt + 1)
                logger.warning(
                    f"【{self.plugin_name}】术语抽取失败，"
                    f"{delay}s 后重试 {attempt + 1}/{self._api_retries}：{e}"
                )
                self._interruptible_sleep(delay)
        raise RuntimeError(f"术语抽取失败，已重试 {self._api_retries} 次：{last_error}")

    def _parse_glossary_response(self, content: str) -> Dict[str, str]:
        return quality_mod.parse_glossary_response(content)

    def _load_glossary_cache(self, cache_key: str) -> Optional[Dict[str, str]]:
        """Load cached glossary_gen output from the existing translation cache directory."""
        if not self._cache_enabled:
            return None
        path = self._translation_cache_path(cache_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = data.get("result") or []
            return {
                str(item.get("term") or "").strip(): str(item.get("translation") or "").strip()
                for item in result
                if str(item.get("term") or "").strip() and str(item.get("translation") or "").strip()
            }
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】读取术语缓存失败：{path}，{e}")
            return None

    def _save_glossary_cache(self, cache_key: str, result: Dict[str, str], ai_config: Dict[str, Any]):
        """Save glossary_gen output using the existing translation cache file layout."""
        if not self._cache_enabled:
            return
        path = self._translation_cache_path(cache_key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "created_at": self._now_text(),
                "source": ai_config.get("source"),
                "model": ai_config.get("model"),
                "target_language": self._target_language,
                "translation_profile": self._translation_profile,
                "result": [
                    {"term": term, "translation": translation}
                    for term, translation in sorted(result.items(), key=lambda item: item[0].lower())
                ],
            }
            tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(path)
            self._cleanup_translation_cache()
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】写入术语缓存失败：{path}，{e}")

    def _build_effective_glossary(self, ai_glossary: Dict[str, str]) -> str:
        """Merge AI and user glossary entries, with user-provided terms taking priority."""
        merged: Dict[str, Tuple[str, str]] = {}
        for term, translation in (ai_glossary or {}).items():
            key = self._glossary_key(term)
            if key and translation:
                merged[key] = (term.strip(), translation.strip())
        user_glossary = self._parse_user_glossary()
        for term, translation in user_glossary.items():
            key = self._glossary_key(term)
            if key and translation:
                merged[key] = (term.strip(), translation.strip())
        lines = [
            f"{term}={translation}"
            for term, translation in sorted(merged.values(), key=lambda item: item[0].lower())
        ]
        raw_user_glossary = (self._glossary or "").strip()
        if raw_user_glossary and not user_glossary:
            lines.append(raw_user_glossary)
        return "\n".join(lines)

    def _parse_user_glossary(self) -> Dict[str, str]:
        """Parse the free-form user glossary textarea into term translations."""
        text = (self._glossary or "").strip()
        if not text:
            return {}
        parsed: Dict[str, str] = {}
        if text.startswith("["):
            try:
                for item in json.loads(self._extract_json_array(text)):
                    term = str(item.get("term") or item.get("source") or "").strip()
                    translation = str(item.get("translation") or item.get("target") or item.get("text") or "").strip()
                    if term and translation:
                        parsed[term] = translation
                return parsed
            except Exception:
                parsed.clear()
        for line in text.splitlines():
            line = line.strip().strip(",;；")
            if not line:
                continue
            for separator in ["=>", "=", "：", ":"]:
                if separator in line:
                    term, translation = line.split(separator, 1)
                    term = term.strip()
                    translation = translation.strip()
                    if term and translation:
                        parsed[term] = translation
                    break
        return parsed

    @staticmethod
    def _glossary_key(term: str) -> str:
        """Normalize glossary terms for duplicate detection and user override matching."""
        return re.sub(r"\s+", " ", term or "").strip().lower()

    def _glossary_prompt(self, media_context: str, glossary: str) -> str:
        """Build the glossary_gen prompt for extracting reusable subtitle terms."""
        return (
            "你是专业影视字幕术语编辑。你必须只返回 JSON 数组，不要 Markdown，不要解释。"
            "数组元素格式为 {\"term\": \"英文术语\", \"translation\": \"简体中文译名\"}。"
            "从输入字幕中抽取需要全片保持一致的专有名词：人名、地名、组织名、作品名、世界观术语、固定称谓和关键技术词。"
            "不要返回普通词、代词、语气词、整句台词或只在单句中出现且不影响一致性的词。"
            "translation 要短、自然、适合影视字幕；不确定时给出最可能的简体中文译名，必要时可保留英文。"
            "同一术语只返回一次；不要输出时间轴，不要输出字幕正文。"
            f"\n影片上下文：{media_context or '未知'}"
            f"\n已有手填术语（优先级更高，避免生成冲突译名）：{glossary or '无'}"
        )

    def _compress_prompt(self, media_context: str, glossary: str) -> str:
        """Build the compress prompt for shortening line-length violations."""
        return (
            "你是专业影视字幕压缩编辑。你必须只返回 JSON 数组，不要 Markdown，不要解释。"
            "数组元素格式为 {\"index\": 数字, \"text\": \"压缩后的译文\"}。"
            "必须保留 index，不要合并、拆分、增删条目；不要输出时间轴，不要输出原文。"
            "输入 items 中的 text 是已润色译文，violations 是超标原因。"
            "任务是在不改变人物、事实、语气和术语的前提下压缩译文。"
            "每条字幕最多两行；中文每行建议不超过 16 字符，英文每行不超过 42 字符；每秒阅读量不超过 15 字符。"
            "可以用一个换行把译文分成两行，但不要超过两行。"
            f"\n影片上下文：{media_context or '未知'}"
            f"\n术语表：{glossary or '无'}"
        )

    def _validate_line_length(
        self,
        cues: List[SubtitleCue],
        media_context: str = "",
        ai_config: Optional[Dict[str, Any]] = None,
        glossary_text: str = "",
    ) -> List[SubtitleCue]:
        """Validate translated subtitle length and compress only entries that exceed limits."""
        if not self._enable_line_check or not self._ai_enabled or not ai_config:
            return cues
        issues = []
        for cue in cues:
            violations = self._line_length_violations(cue)
            if violations:
                issues.append((cue, violations))
        if not issues:
            return cues

        logger.info(f"【{self.plugin_name}】字幕长度校验发现超标条目：{len(issues)} 条，开始压缩")
        compressed = self._compress_line_length_issues(issues, media_context, ai_config, glossary_text)
        if not compressed:
            return cues

        output = []
        for cue in cues:
            text = compressed.get(cue.index)
            if text:
                output.append(SubtitleCue(
                    index=cue.index,
                    start=cue.start,
                    end=cue.end,
                    text=text,
                    line_index=cue.line_index,
                    ass_fields=list(cue.ass_fields) if cue.ass_fields else None,
                    ass_text_index=cue.ass_text_index,
                ))
            else:
                output.append(cue)
        return output

    def _line_length_violations(self, cue: SubtitleCue) -> List[str]:
        """Return Netflix-style line count, line length, and CPS violations for one cue."""
        text = self._plain_subtitle_text(cue.text)
        if not text:
            return []
        lines = [line.strip() for line in text.replace("\\N", "\n").splitlines() if line.strip()]
        if not lines:
            return []

        violations = []
        chinese_limit = bool(re.search(r"[\u4e00-\u9fff]", text))
        line_limit = 16 if chinese_limit else 42
        if len(lines) > 2:
            violations.append(f"超过两行：{len(lines)} 行")
        for line_no, line in enumerate(lines, start=1):
            if len(line) > line_limit:
                violations.append(f"第 {line_no} 行 {len(line)} 字符，限制 {line_limit}")
        duration = self._cue_duration_seconds(cue)
        readable_chars = len(re.sub(r"\s+", "", text))
        if duration > 0:
            cps = readable_chars / duration
            if cps > 15:
                violations.append(f"CPS {cps:.1f}，限制 15")
        return violations

    def _compress_line_length_issues(
        self,
        issues: List[Tuple[SubtitleCue, List[str]]],
        media_context: str,
        ai_config: Dict[str, Any],
        glossary_text: str,
    ) -> Dict[int, str]:
        """Run the cached compress stage and return successful per-index replacements."""
        items = []
        for cue, violations in issues:
            duration = self._cue_duration_seconds(cue)
            items.append({
                "index": cue.index,
                "text": self._plain_subtitle_text(cue.text),
                "violations": "；".join(violations),
                "duration_seconds": round(duration, 3) if duration > 0 else 0,
                "limits": "中文每行<=16；英文每行<=42；每条<=2行；CPS<=15",
            })
        try:
            return self._translate_stage(
                stage="compress",
                items=items,
                media_context=media_context,
                previous=None,
                ai_config=ai_config,
                glossary_text=glossary_text,
            )
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】字幕压缩失败，保留润色译文继续输出：{e}")
            return {}

    def _cue_duration_seconds(self, cue: SubtitleCue) -> float:
        """Calculate cue duration in seconds from SRT or ASS timestamp strings."""
        start = self._parse_subtitle_time(cue.start)
        end = self._parse_subtitle_time(cue.end)
        if start is None or end is None:
            return 0.0
        return max(end - start, 0.0)

    @staticmethod
    def _parse_subtitle_time(value: str) -> Optional[float]:
        """Parse SRT/ASS timestamps such as 00:01:02,345 or 0:01:02.34."""
        match = re.match(r"^\s*(\d+):(\d{1,2}):(\d{1,2})(?:[,.](\d+))?\s*$", value or "")
        if not match:
            return None
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        fraction = match.group(4) or ""
        fraction_seconds = float(f"0.{fraction}") if fraction else 0.0
        return hours * 3600 + minutes * 60 + seconds + fraction_seconds

    @staticmethod
    def _extract_json_array(content: str) -> str:
        return formats_mod.extract_json_array(content)

    @staticmethod
    def _is_sentence_boundary(text: str) -> bool:
        return formats_mod.is_sentence_boundary(text)

    def _parse_srt(self, content: str) -> List[SubtitleCue]:
        return formats_mod.parse_srt(content)

    @staticmethod
    def _render_srt(cues: List[SubtitleCue]) -> str:
        return formats_mod.render_srt(cues)

    def _parse_ass(self, content: str) -> Tuple[List[str], List[SubtitleCue]]:
        return formats_mod.parse_ass(content)

    @staticmethod
    def _render_ass(lines: List[str], cues: List[SubtitleCue]) -> str:
        return formats_mod.render_ass(lines, cues)

    @staticmethod
    def _plain_subtitle_text(text: str) -> str:
        return formats_mod.plain_subtitle_text(text)

    def _translated_subtitle_path(self, video_path: Path, source_path: Path) -> Path:
        suffix = source_path.suffix.lower()
        if suffix not in self._TRANSLATABLE_EXTS:
            suffix = ".srt"
        parts = [video_path.stem, self._target_language]
        if self._translation_suffix:
            parts.append(self._translation_suffix)
        return video_path.with_name(f"{'.'.join(parts)}{suffix}")

    def _is_chinese_language(self, language: str, title: str = "", path: Optional[Path] = None) -> bool:
        return language_mod.is_chinese_language(language, title, path)

    def _is_english_language(self, language: str, title: str = "", path: Optional[Path] = None) -> bool:
        return language_mod.is_english_language(language, title, path)

    def _language_from_text(self, text: str) -> str:
        return language_mod.language_from_text(text, self._target_language)

    def _normalize_language(self, value: str) -> str:
        return language_mod.normalize_language(value)

    def _build_media_context(self, mediainfo: Optional[MediaInfo], target: Path) -> str:
        fields = []
        if mediainfo:
            for attr in [
                "title", "title_year", "en_title", "original_title", "year",
                "release_date", "original_language", "imdb_id", "tmdb_id",
                "tvdb_id", "type", "season", "episode", "overview",
                "names", "genres", "directors", "actors",
            ]:
                value = getattr(mediainfo, attr, None)
                if value:
                    fields.append(f"{attr}={value}")
        nfo_context = self._extract_context_from_nfo(target)
        if nfo_context:
            fields.append(f"nfo={nfo_context}")
        return "；".join(fields)[:4000]

    def _extract_context_from_nfo(self, target: Path) -> str:
        base = target.parent if target.is_file() else target
        if not base.exists() or not base.is_dir():
            return ""
        chunks = []
        for nfo in list(base.glob("*.nfo"))[:3]:
            try:
                content = nfo.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for tag in ["title", "originaltitle", "year", "plot", "outline", "imdbid", "tmdbid"]:
                match = re.search(fr"<{tag}>(.*?)</{tag}>", content, flags=re.IGNORECASE | re.DOTALL)
                if match:
                    value = re.sub(r"\s+", " ", match.group(1)).strip()
                    if value:
                        chunks.append(f"{tag}:{value}")
        return "；".join(chunks)[:2000]

    @staticmethod
    def _media_title(mediainfo: Optional[MediaInfo], target: Path) -> str:
        if mediainfo:
            return (
                getattr(mediainfo, "title_year", None)
                or getattr(mediainfo, "title", None)
                or target.stem
            )
        return target.stem or target.name or str(target)

    @staticmethod
    def _resolve_target_path(target_path: str) -> Path:
        return Path(target_path).expanduser()

    @staticmethod
    def _split_target_paths(target_path: Any) -> List[str]:
        text = str(target_path or "").strip()
        if not text:
            return []
        return [item.strip() for item in re.split(r"[,，]", text) if item.strip()]

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value).strip() if value is not None else ""

    def _resolve_ai_config(self) -> Tuple[Optional[Dict[str, Any]], str]:
        if not self._ai_enabled:
            return None, "AI 翻译未启用"

        if self._model_source == "system":
            config = {
                "source": "system",
                "provider": self._clean_text(getattr(settings, "LLM_PROVIDER", None)) or "openai",
                "model": self._clean_text(getattr(settings, "LLM_MODEL", None)),
                "api_key": self._clean_text(getattr(settings, "LLM_API_KEY", None)),
                "base_url": self._clean_text(getattr(settings, "LLM_BASE_URL", None)),
                "user_agent": self._clean_text(getattr(settings, "LLM_USER_AGENT", None)) or None,
                "use_proxy": bool(getattr(settings, "LLM_USE_PROXY", True)),
            }
        else:
            config = {
                "source": "custom",
                "provider": "openai",
                "model": self._clean_text(self._api_model),
                "api_key": self._clean_text(self._api_key),
                "base_url": self._clean_text(self._api_base_url),
                "user_agent": None,
                "use_proxy": bool(self._api_use_proxy),
            }

        if not config["api_key"]:
            return None, "未配置 LLM API Key"
        if not config["model"]:
            return None, "未配置 LLM 模型 ID"
        if not config["base_url"]:
            return None, "未配置 LLM Base URL"
        return config, ""

    @staticmethod
    def _safe_int(value: Any, default: int, min_value: int = 0, max_value: int = 999999) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(min_value, min(number, max_value))

    def _translation_profile_label(self) -> str:
        """Return a Chinese label for the current translation profile."""
        labels = {
            "fast": "快速",
            "standard": "标准",
            "quality": "质量优先",
        }
        label = labels.get(self._translation_profile, self._translation_profile)
        return f"{label}({self._translation_profile})"

    def _estimate_scan_subtitle_chars(self, tracks: List[SubtitleTrack]) -> int:
        """Estimate subtitle text size from scanned text-based external subtitle files using actual UTF-8 character count."""
        total = 0
        for track in tracks:
            if not track.text_based or not track.path:
                continue
            try:
                if track.path.exists():
                    total += len(track.path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
        return total

    def _estimate_eta_seconds(self, video_count, total_chars) -> int:
        return eta_mod.estimate_eta_seconds(
            video_count,
            total_chars,
            translation_profile=self._translation_profile,
            batch_chars=self._batch_chars,
            parallel_batches=self._parallel_batches,
        )

    def _format_duration(self, seconds: int) -> str:
        return eta_mod.format_duration(seconds)

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
