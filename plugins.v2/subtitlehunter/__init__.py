# _*_ coding: utf-8 _*_
import hashlib
import json
import re
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


@dataclass
class SubtitleTrack:
    source: str
    path: Optional[Path]
    video_path: Optional[Path]
    stream_index: Optional[int]
    codec: str
    language: str
    title: str
    forced: bool
    default: bool
    text_based: bool
    extension: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": str(self.path) if self.path else "",
            "video_path": str(self.video_path) if self.video_path else "",
            "stream_index": self.stream_index,
            "codec": self.codec,
            "language": self.language,
            "title": self.title,
            "forced": self.forced,
            "default": self.default,
            "text_based": self.text_based,
            "extension": self.extension,
        }


@dataclass
class SubtitleCue:
    index: int
    start: str
    end: str
    text: str
    line_index: Optional[int] = None
    ass_fields: Optional[List[str]] = None
    ass_text_index: Optional[int] = None


class SubtitleHunter(_PluginBase):
    plugin_name = "SubtitleHunter"
    plugin_desc = "入库后自动检测、提取、翻译并规范化字幕"
    plugin_icon = "subtitle.png"
    plugin_version = "2.4"
    plugin_author = "milikii"
    author_url = "https://github.com/milikii"
    plugin_config_prefix = "subtitle_hunter_"
    plugin_order = 20
    auth_level = 1

    _VIDEO_EXTS = {
        ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".flv",
        ".ts", ".m2ts", ".mts", ".webm",
    }
    _SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
    _TRANSLATABLE_EXTS = {".srt", ".ass", ".ssa"}
    _TEXT_CODECS = {
        "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text",
        "realtext", "microdvd", "mpl2", "sami",
    }
    _IMAGE_CODECS = {
        "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub",
    }
    _CHI_PATTERNS = re.compile(
        r"((^|[._\-\s\[\]()])"
        r"(chi|chs|cht|chinese|zh([_-]?(cn|hans|hant|tw|hk))?|zho|cmn)"
        r"(?=$|[._\-\s\[\]()]))|中文|简体|繁体|简中|繁中|双语|中英",
        re.IGNORECASE,
    )
    _ENG_PATTERNS = re.compile(
        r"((^|[._\-\s\[\]()])(eng|en|english)(?=$|[._\-\s\[\]()]))|英文|英语",
        re.IGNORECASE,
    )
    _FORCED_PATTERNS = re.compile(
        r"(^|[._\-\s\[\]()])(forced|foreign|only|signs?)(?=$|[._\-\s\[\]()])|强制|特效",
        re.IGNORECASE,
    )
    _SRT_TIME = re.compile(
        r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
        r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})(?P<tail>.*)"
    )

    def init_plugin(self, config: dict = None):
        self._enabled = False
        self._notify = True
        self._onlyonce = False
        self._target_path = ""
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
        self._api_retries = 3
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
            self._api_retries = self._safe_int(config.get("api_retries"), 3, 0, 10)
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

        if self._onlyonce:
            self._onlyonce = False
            self.update_config(self._current_config(onlyonce=False))
            if self._target_path:
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
        return []

    def stop_service(self):
        pass

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
        ]

    def get_status(self) -> Dict[str, Any]:
        return self._runtime_snapshot()

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
        target = self._resolve_target_path(path or self._target_path)
        result = self._scan_target(target)
        return {
            "success": True,
            "target": str(target),
            "videos": [str(video) for video in result["videos"]],
            "subtitles": [track.to_dict() for track in result["subtitles"]],
            "errors": result["errors"],
        }

    def api_ensure_chinese(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        target_path = payload.get("path") or self._target_path
        if not target_path:
            return {"success": False, "message": "未指定 path"}
        self._start_background_job(
            source="API确保中文字幕",
            target_path=target_path,
            mediainfo=None,
        )
        return {"success": True, "message": "任务已提交", "target": target_path}

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

    def _start_background_job(
        self,
        source: str,
        target_path: str,
        mediainfo: Optional[MediaInfo],
    ):
        target = self._resolve_target_path(target_path)
        title = self._media_title(mediainfo, target)
        threading.Thread(
            target=self._ensure_chinese_workflow,
            args=(source, target, title, mediainfo),
            daemon=True,
            name=f"SubtitleHunter-{title}",
        ).start()

    def _ensure_chinese_workflow(
        self,
        source: str,
        target: Path,
        display_name: str,
        mediainfo: Optional[MediaInfo],
    ):
        started_at = self._start_run(source=source, target=target, display_name=display_name)
        try:
            if not target.exists():
                message = f"目标不存在：{target}"
                logger.error(f"【{self.plugin_name}】{message}")
                self._finish_run("失败", message, started_at, error=message)
                return

            media_context = self._build_media_context(mediainfo, target)
            scan = self._scan_target(target)
            videos = scan["videos"]
            if not videos:
                message = f"未发现视频文件：{target}"
                logger.warning(f"【{self.plugin_name}】{message}")
                self._finish_run("已跳过", message, started_at, errors=scan["errors"])
                return

            self._update_run(
                videos=len(videos),
                subtitles=len(scan["subtitles"]),
                message=f"发现 {len(videos)} 个视频，开始处理字幕",
                errors=scan["errors"],
            )

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
                detail = self._ensure_video_chinese(video, media_context)
                details.append(detail)
                summary["processed"] += 1
                summary["skipped"] += 1 if detail["status"] in {"已有中文", "已跳过"} else 0
                summary["extracted"] += len(detail.get("extracted_files", []))
                summary["translated"] += len(detail.get("translated_files", []))
                summary["renamed"] += len(detail.get("renamed_files", []))
                summary["failed"] += 1 if detail["status"] == "失败" else 0
                self._update_run(
                    processed=summary["processed"],
                    skipped=summary["skipped"],
                    extracted=summary["extracted"],
                    translated=summary["translated"],
                    renamed=summary["renamed"],
                    failed=summary["failed"],
                    last_video=str(video),
                    details=details[-10:],
                    message=f"处理中：{summary['processed']}/{len(videos)}",
                )

            final_status = "完成" if summary["failed"] == 0 else "部分失败"
            message = (
                f"处理完成：视频 {summary['processed']}，跳过 {summary['skipped']}，"
                f"提取 {summary['extracted']}，翻译 {summary['translated']}，"
                f"重命名 {summary['renamed']}，失败 {summary['failed']}"
            )
            self._finish_run(final_status, message, started_at, details=details, **summary)
            self._send_notify(final_status, message)

        except Exception as e:
            message = f"{display_name} 字幕处理失败：{e}"
            logger.error(f"【{self.plugin_name}】{message}\n{traceback.format_exc()}")
            self._finish_run("失败", message, started_at, error=traceback.format_exc())
            self._send_notify("字幕处理失败", message)

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

            chinese_tracks = [track for track in all_tracks if self._is_chinese_language(track.language, track.title, track.path)]
            external_chinese = [track for track in chinese_tracks if track.source == "external"]
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
            if translated_path.exists() and not self._overwrite:
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

    def _scan_target(self, target: Path) -> Dict[str, Any]:
        result = {"videos": [], "subtitles": [], "errors": []}
        if not target:
            result["errors"].append("未指定目标路径")
            return result

        videos = self._find_videos(target)
        result["videos"] = videos

        for video in videos:
            result["subtitles"].extend(self._find_external_subtitles(video))
            try:
                result["subtitles"].extend(self._probe_embedded_subtitles(video))
            except Exception as e:
                result["errors"].append(f"{video}: {e}")

        if target.is_dir():
            associated = {str(track.path) for track in result["subtitles"] if track.path}
            for subtitle in sorted(target.rglob("*")):
                if subtitle.is_file() and subtitle.suffix.lower() in self._SUB_EXTS:
                    if str(subtitle) not in associated:
                        result["subtitles"].append(self._external_track_from_path(subtitle, None))

        return result

    def _find_videos(self, target: Path) -> List[Path]:
        if target.is_file() and target.suffix.lower() in self._VIDEO_EXTS:
            return [target]
        if not target.is_dir():
            return []
        return sorted(
            path for path in target.rglob("*")
            if path.is_file() and path.suffix.lower() in self._VIDEO_EXTS
        )

    def _find_external_subtitles(self, video_path: Path) -> List[SubtitleTrack]:
        tracks = []
        for subtitle in sorted(video_path.parent.iterdir()):
            if not subtitle.is_file() or subtitle.suffix.lower() not in self._SUB_EXTS:
                continue
            if not self._subtitle_belongs_to_video(subtitle, video_path):
                continue
            tracks.append(self._external_track_from_path(subtitle, video_path))
        return tracks

    def _subtitle_belongs_to_video(self, subtitle_path: Path, video_path: Path) -> bool:
        if subtitle_path.stem == video_path.stem:
            return True
        if subtitle_path.stem.startswith(f"{video_path.stem}."):
            return True
        video_count = sum(1 for path in video_path.parent.iterdir() if path.is_file() and path.suffix.lower() in self._VIDEO_EXTS)
        return video_count == 1

    def _external_track_from_path(self, subtitle_path: Path, video_path: Optional[Path]) -> SubtitleTrack:
        language = self._language_from_text(subtitle_path.stem)
        return SubtitleTrack(
            source="external",
            path=subtitle_path,
            video_path=video_path,
            stream_index=None,
            codec=subtitle_path.suffix.lower().lstrip("."),
            language=language,
            title=subtitle_path.stem,
            forced=bool(self._FORCED_PATTERNS.search(subtitle_path.stem)),
            default=False,
            text_based=subtitle_path.suffix.lower() in self._TRANSLATABLE_EXTS,
            extension=subtitle_path.suffix.lower(),
        )

    def _probe_embedded_subtitles(self, video_path: Path) -> List[SubtitleTrack]:
        ok, stdout, stderr = self._run_command([
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            str(video_path),
        ], timeout=self._ffmpeg_timeout)
        if not ok:
            raise RuntimeError(f"ffprobe 失败：{stderr or stdout}")

        payload = json.loads(stdout or "{}")
        tracks = []
        for stream in payload.get("streams", []):
            if stream.get("codec_type") != "subtitle":
                continue
            tags = stream.get("tags") or {}
            disposition = stream.get("disposition") or {}
            codec = (stream.get("codec_name") or "").lower()
            language = self._normalize_language(tags.get("language") or "")
            title = tags.get("title") or stream.get("codec_long_name") or ""
            extension = self._extension_for_codec(codec)
            tracks.append(SubtitleTrack(
                source="embedded",
                path=None,
                video_path=video_path,
                stream_index=stream.get("index"),
                codec=codec,
                language=language or self._language_from_text(" ".join([title, codec])),
                title=title,
                forced=bool(disposition.get("forced")) or bool(self._FORCED_PATTERNS.search(title)),
                default=bool(disposition.get("default")),
                text_based=codec in self._TEXT_CODECS,
                extension=extension,
            ))
        return tracks

    def _extract_embedded_subtitle(self, track: SubtitleTrack) -> Tuple[bool, Optional[Path], str]:
        if not track.video_path or track.stream_index is None:
            return False, None, "缺少视频路径或字幕流索引"
        if not track.text_based:
            return False, None, "图形字幕无法直接提取为 srt/ass"

        output = self._extracted_subtitle_path(track)
        if output.exists() and not self._overwrite:
            return True, output, f"字幕已存在：{output}"

        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-nostdin",
            "-y" if self._overwrite else "-n",
            "-i", str(track.video_path),
            "-map", f"0:{track.stream_index}",
            str(output),
        ]
        ok, stdout, stderr = self._run_command(command, timeout=self._ffmpeg_timeout)
        if ok and output.exists():
            logger.info(f"【{self.plugin_name}】已提取字幕：{track.video_path} stream {track.stream_index} -> {output}")
            return True, output, f"提取成功：{output}"
        return False, output, f"ffmpeg 提取失败：{stderr or stdout}"

    def _rename_external_subtitles(self, video_path: Path, tracks: List[SubtitleTrack]) -> List[str]:
        renamed = []
        for track in tracks:
            if not track.path or not track.path.exists():
                continue
            language = self._subtitle_language_for_name(track)
            if not language:
                continue
            suffixes = [language]
            if track.forced:
                suffixes.append("forced")
            if self._translation_suffix and self._translation_suffix in track.path.stem.split("."):
                suffixes.append(self._translation_suffix)
            dest = video_path.with_name(f"{video_path.stem}.{'.'.join(suffixes)}{track.path.suffix.lower()}")
            if dest == track.path:
                continue
            if dest.exists() and not self._overwrite:
                continue
            try:
                track.path.rename(dest)
                renamed.append(f"{track.path} -> {dest}")
                logger.info(f"【{self.plugin_name}】字幕重命名：{track.path} -> {dest}")
            except Exception as e:
                logger.warning(f"【{self.plugin_name}】字幕重命名失败：{track.path} -> {dest}，{e}")
        return renamed

    def _translate_subtitle_file(
        self,
        source_path: Path,
        output_path: Path,
        media_context: str,
        ai_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        suffix = source_path.suffix.lower()
        try:
            content = source_path.read_text(encoding="utf-8-sig", errors="ignore")
            if suffix == ".srt":
                cues = self._parse_srt(content)
                translated = self._translate_cues(cues, media_context, ai_config)
                output_path.write_text(self._render_srt(translated), encoding="utf-8")
            elif suffix in {".ass", ".ssa"}:
                lines, cues = self._parse_ass(content)
                translated = self._translate_cues(cues, media_context, ai_config)
                output_path.write_text(self._render_ass(lines, translated), encoding="utf-8")
            else:
                return False, f"暂不支持翻译 {suffix} 字幕"
            logger.info(f"【{self.plugin_name}】字幕翻译完成：{source_path} -> {output_path}")
            return True, f"翻译完成：{output_path}"
        except Exception as e:
            logger.error(f"【{self.plugin_name}】字幕翻译失败：{source_path}，{e}\n{traceback.format_exc()}")
            return False, f"字幕翻译失败：{e}"

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
        last_error = None
        attempts = self._api_retries + 1

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
            except Exception as e:
                last_error = e
                if attempt >= attempts - 1:
                    break
                delay = max(1, self._api_timeout // 60) * (attempt + 1)
                logger.warning(
                    f"【{self.plugin_name}】{stage_name}失败，"
                    f"{delay}s 后重试 {attempt + 1}/{self._api_retries}：{e}"
                )
                time.sleep(delay)

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
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】写入翻译缓存失败：{path}，{e}")

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
        data = json.loads(self._extract_json_array(content))
        result = {}
        for item in data:
            index = int(item.get("index"))
            value = str(item.get("text") or "").strip()
            result[index] = value
        return result

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
        last_error = None
        attempts = self._api_retries + 1
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
            except Exception as e:
                last_error = e
                if attempt >= attempts - 1:
                    break
                delay = max(1, self._api_timeout // 60) * (attempt + 1)
                logger.warning(
                    f"【{self.plugin_name}】术语抽取失败，"
                    f"{delay}s 后重试 {attempt + 1}/{self._api_retries}：{e}"
                )
                time.sleep(delay)
        raise RuntimeError(f"术语抽取失败，已重试 {self._api_retries} 次：{last_error}")

    def _parse_glossary_response(self, content: str) -> Dict[str, str]:
        """Parse a glossary_gen JSON array into a term-to-translation mapping."""
        data = json.loads(self._extract_json_array(content))
        result: Dict[str, str] = {}
        for item in data:
            term = str(item.get("term") or "").strip()
            translation = str(item.get("translation") or "").strip()
            if term and translation:
                result[term] = translation
        return result

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
        """Extract the first JSON array from a model response, allowing fenced JSON."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"```$", "", text).strip()
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            return text[start:end + 1]
        return text

    @staticmethod
    def _is_sentence_boundary(text: str) -> bool:
        """Return True when subtitle text ends with a sentence-ending punctuation mark."""
        cleaned = SubtitleHunter._plain_subtitle_text(text)
        return bool(re.search(r"(?:\.{3}|[.!?。！？]|…+)[\"'）)\]\}”’]*\s*$", cleaned))

    def _parse_srt(self, content: str) -> List[SubtitleCue]:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []
        cues = []
        for block_no, block in enumerate(re.split(r"\n\s*\n", normalized), start=1):
            lines = [line for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            index = block_no
            if lines[0].strip().isdigit():
                index = int(lines[0].strip())
                lines = lines[1:]
            if not lines:
                continue
            match = self._SRT_TIME.match(lines[0].strip())
            if not match:
                continue
            cues.append(SubtitleCue(
                index=index,
                start=match.group("start").replace(".", ","),
                end=match.group("end").replace(".", ","),
                text="\n".join(lines[1:]).strip(),
            ))
        return cues

    @staticmethod
    def _render_srt(cues: List[SubtitleCue]) -> str:
        blocks = []
        for output_index, cue in enumerate(cues, start=1):
            text = (cue.text or "").strip()
            blocks.append(f"{output_index}\n{cue.start} --> {cue.end}\n{text}")
        return "\n\n".join(blocks) + "\n"

    def _parse_ass(self, content: str) -> Tuple[List[str], List[SubtitleCue]]:
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        cues = []
        in_events = False
        format_fields: List[str] = []
        text_index = -1
        cue_index = 1

        for line_no, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower() == "[events]":
                in_events = True
                continue
            if in_events and stripped.startswith("[") and stripped.endswith("]"):
                in_events = False
            if not in_events:
                continue
            if stripped.lower().startswith("format:"):
                format_fields = [part.strip().lower() for part in stripped.split(":", 1)[1].split(",")]
                try:
                    text_index = format_fields.index("text")
                except ValueError:
                    text_index = -1
                continue
            if not stripped.lower().startswith("dialogue:") or text_index < 0:
                continue
            raw = line.split(":", 1)[1].lstrip()
            fields = raw.split(",", len(format_fields) - 1)
            if len(fields) <= text_index:
                continue
            start = fields[format_fields.index("start")] if "start" in format_fields else ""
            end = fields[format_fields.index("end")] if "end" in format_fields else ""
            cues.append(SubtitleCue(
                index=cue_index,
                start=start,
                end=end,
                text=fields[text_index].replace("\\N", "\n"),
                line_index=line_no,
                ass_fields=fields,
                ass_text_index=text_index,
            ))
            cue_index += 1
        return lines, cues

    @staticmethod
    def _render_ass(lines: List[str], cues: List[SubtitleCue]) -> str:
        for cue in cues:
            if cue.line_index is None or cue.ass_fields is None or cue.ass_text_index is None:
                continue
            fields = list(cue.ass_fields)
            fields[cue.ass_text_index] = (cue.text or "").replace("\n", "\\N")
            lines[cue.line_index] = "Dialogue: " + ",".join(fields)
        return "\n".join(lines)

    @staticmethod
    def _plain_subtitle_text(text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", "", text or "")
        cleaned = re.sub(r"\{[^}]*}", "", cleaned)
        cleaned = cleaned.replace("\\N", "\n")
        return cleaned.strip()

    def _translated_subtitle_path(self, video_path: Path, source_path: Path) -> Path:
        suffix = source_path.suffix.lower()
        if suffix not in self._TRANSLATABLE_EXTS:
            suffix = ".srt"
        parts = [video_path.stem, self._target_language]
        if self._translation_suffix:
            parts.append(self._translation_suffix)
        return video_path.with_name(f"{'.'.join(parts)}{suffix}")

    def _extracted_subtitle_path(self, track: SubtitleTrack) -> Path:
        video_path = track.video_path
        language = self._subtitle_language_for_name(track) or "und"
        parts = [video_path.stem, language]
        if track.forced:
            parts.append("forced")
        parts.append(f"stream{track.stream_index}")
        return video_path.with_name(f"{'.'.join(parts)}{track.extension}")

    def _subtitle_language_for_name(self, track: SubtitleTrack) -> str:
        if self._is_chinese_language(track.language, track.title, track.path):
            return self._target_language
        if self._is_english_language(track.language, track.title, track.path):
            return "en"
        return self._normalize_language(track.language) or ""

    def _extension_for_codec(self, codec: str) -> str:
        codec = (codec or "").lower()
        if codec in {"ass", "ssa"}:
            return ".ass"
        if codec in self._IMAGE_CODECS:
            return ".sup"
        return ".srt"

    def _is_chinese_language(self, language: str, title: str = "", path: Optional[Path] = None) -> bool:
        text = " ".join(filter(None, [language or "", title or "", path.name if path else ""]))
        return self._normalize_language(language) in {"zh", "zh-Hans", "zh-Hant"} or bool(self._CHI_PATTERNS.search(text))

    def _is_english_language(self, language: str, title: str = "", path: Optional[Path] = None) -> bool:
        text = " ".join(filter(None, [language or "", title or "", path.name if path else ""]))
        return self._normalize_language(language) == "en" or bool(self._ENG_PATTERNS.search(text))

    def _language_from_text(self, text: str) -> str:
        if self._CHI_PATTERNS.search(text or ""):
            return self._target_language
        if self._ENG_PATTERNS.search(text or ""):
            return "en"
        return ""

    def _normalize_language(self, value: str) -> str:
        lang = (value or "").strip().lower().replace("_", "-")
        if not lang:
            return ""
        if lang in {"zh", "chi", "zho", "chs", "cmn", "zh-cn", "zh-hans", "cn", "chinese"}:
            return "zh-Hans"
        if lang in {"cht", "zh-tw", "zh-hk", "zh-hant"}:
            return "zh-Hant"
        if lang in {"en", "eng", "english"}:
            return "en"
        return lang

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

    @staticmethod
    def _run_command(command: List[str], timeout: int) -> Tuple[bool, str, str]:
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.returncode == 0, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError as e:
            return False, "", f"命令不存在：{command[0]}，{e}"
        except subprocess.TimeoutExpired as e:
            return False, e.stdout or "", f"命令超时：{command[0]}"

    def _send_notify(self, title: str, message: str):
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"{self.plugin_name}：{title}",
                text=message,
            )
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】发送通知失败：{e}")

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
            "videos": 0,
            "subtitles": 0,
            "processed": 0,
            "skipped": 0,
            "extracted": 0,
            "translated": 0,
            "renamed": 0,
            "failed": 0,
            "last_video": "-",
            "message": "暂无运行记录",
            "error": "",
            "errors": [],
            "details": [],
            "translation_batches_total": 0,
            "translation_batches_done": 0,
        }
        self._history = []

    def _ensure_runtime_status(self):
        if not hasattr(self, "_status_lock"):
            self._init_runtime_status()

    def _start_run(self, source: str, target: Path, display_name: str) -> float:
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
                "target_path": str(target),
                "media": display_name,
                "videos": 0,
                "subtitles": 0,
                "processed": 0,
                "skipped": 0,
                "extracted": 0,
                "translated": 0,
                "renamed": 0,
                "failed": 0,
                "last_video": "-",
                "message": "开始处理",
                "error": "",
                "errors": [],
                "details": [],
                "translation_batches_total": 0,
                "translation_batches_done": 0,
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
                "message": message,
            }
            self._history.insert(0, record)
            self._history = self._history[:10]

    def _runtime_snapshot(self) -> Dict[str, Any]:
        self._ensure_runtime_status()
        with self._status_lock:
            snapshot = dict(self._runtime)
            snapshot["enabled"] = self._enabled if hasattr(self, "_enabled") else False
            snapshot["ai_enabled"] = self._ai_enabled if hasattr(self, "_ai_enabled") else False
            snapshot["model_source"] = self._model_source if hasattr(self, "_model_source") else "system"
            snapshot["translation_profile"] = self._translation_profile if hasattr(self, "_translation_profile") else "quality"
            snapshot["parallel_batches"] = self._parallel_batches if hasattr(self, "_parallel_batches") else 1
            snapshot["batch_size"] = self._batch_size if hasattr(self, "_batch_size") else 60
            snapshot["batch_chars"] = self._batch_chars if hasattr(self, "_batch_chars") else 9000
            ai_config, ai_error = self._resolve_ai_config() if snapshot["ai_enabled"] else (None, "AI 翻译未启用")
            snapshot["ai_ready"] = bool(ai_config)
            snapshot["ai_error"] = ai_error
            snapshot["ai_model"] = ai_config.get("model") if ai_config else ""
            snapshot["ai_base_url"] = ai_config.get("base_url") if ai_config else ""
            snapshot["history"] = [dict(item) for item in self._history]
            return snapshot

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _current_config(self, onlyonce: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
            "target_path": self._target_path,
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
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "auto_ensure", "label": "入库后自动确保中文"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_retries",
                                        "label": "API 重试次数",
                                        "type": "number",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "cache_enabled",
                                        "label": "启用翻译缓存",
                                        "hint": "缓存保存在插件数据目录，用于失败后续跑",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "target_path",
                                        "label": "媒体目录或视频路径",
                                        "placeholder": "/media/Movies/Movie.Name.2026/Movie.Name.2026.mkv",
                                        "hint": "用于立即运行一次，也可通过 API 指定 path",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "rename_existing", "label": "规范化外挂字幕命名"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "extract_chinese_embedded", "label": "提取内嵌中文字幕"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "overwrite", "label": "覆盖已有字幕"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "ai_enabled", "label": "启用 AI 翻译"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "model_source",
                                        "label": "模型来源",
                                        "items": [
                                            {"title": "复用系统智能助手", "value": "system"},
                                            {"title": "自定义 OpenAI API", "value": "custom"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_base_url",
                                        "label": "自定义 API Base URL",
                                        "placeholder": "https://api.openai.com/v1",
                                        "hint": "模型来源为自定义时生效",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_model",
                                        "label": "自定义模型名",
                                        "hint": "模型来源为自定义时生效",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_timeout",
                                        "label": "API 超时秒数",
                                        "type": "number",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_key",
                                        "label": "自定义 API Key",
                                        "type": "password",
                                        "hint": "模型来源为自定义时生效；复用系统智能助手时不会读取这里",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "api_use_proxy",
                                        "label": "自定义 API 使用代理",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "target_language",
                                        "label": "中文字幕语言标记",
                                        "placeholder": "zh-Hans",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "translation_suffix",
                                        "label": "翻译字幕后缀",
                                        "placeholder": "ai",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "translation_profile",
                                        "label": "翻译模式",
                                        "items": [
                                            {"title": "影院质量：三段", "value": "quality"},
                                            {"title": "标准速度：两段", "value": "standard"},
                                            {"title": "最快速度：一段", "value": "fast"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "parallel_batches",
                                        "label": "并发批次数",
                                        "type": "number",
                                        "hint": "建议 3-6；网关稳定且额度充足时再提高",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "batch_size",
                                        "label": "每批字幕条数",
                                        "type": "number",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "batch_chars",
                                        "label": "每批最大字符",
                                        "type": "number",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_line_check",
                                        "label": "启用行长度校验",
                                        "hint": "按 Netflix 行长和 CPS 标准压缩超长译文",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "glossary",
                                        "label": "术语表（可选）",
                                        "placeholder": "不维护术语表可以留空；例如：Stark=史塔克",
                                        "rows": 4,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "工作流：检测中文字幕；没有则提取内嵌英文文本字幕；再按所选模式并发翻译为同目录外挂中文字幕。插件只写外挂字幕，不修改视频文件。",
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], self._current_config(onlyonce=False)

    def get_page(self) -> List[dict]:
        status = self._runtime_snapshot()
        alert_type = {
            "完成": "success",
            "已有中文": "success",
            "已生成中文": "success",
            "失败": "error",
            "部分失败": "warning",
            "配置错误": "warning",
            "已跳过": "info",
            "运行中": "info",
        }.get(status.get("status"), "info")

        detail_rows = self._build_table_rows([
            ("插件开关", "已启用" if status.get("enabled") else "未启用"),
            ("AI 翻译", "已启用" if status.get("ai_enabled") else "未启用"),
            ("运行状态", status.get("status", "-")),
            ("最近开始", status.get("started_at", "-")),
            ("最近结束", status.get("finished_at", "-")),
            ("耗时", status.get("duration", "-")),
            ("来源", status.get("source", "-")),
            ("媒体", status.get("media", "-")),
            ("目标", status.get("target_path", "-")),
            ("翻译模式", status.get("translation_profile", "-")),
            ("并发批次", status.get("parallel_batches", "-")),
            ("批次大小", f"{status.get('batch_size', '-')}/{status.get('batch_chars', '-')} chars"),
            ("批次进度", f"{status.get('translation_batches_done', 0)}/{status.get('translation_batches_total', 0)}"),
            ("视频数", status.get("videos", 0)),
            ("字幕数", status.get("subtitles", 0)),
            ("已处理", status.get("processed", 0)),
            ("已跳过", status.get("skipped", 0)),
            ("已提取", status.get("extracted", 0)),
            ("已翻译", status.get("translated", 0)),
            ("已重命名", status.get("renamed", 0)),
            ("失败", status.get("failed", 0)),
            ("最近视频", status.get("last_video", "-")),
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
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        if not history_rows:
            history_rows = [{
                "component": "tr",
                "content": [
                    {
                        "component": "td",
                        "props": {"colspan": 5},
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
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": alert_type,
                                "variant": "tonal",
                                "text": f"{status.get('status', '未运行')}：{status.get('message', '')}",
                            },
                        }],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VTable",
                            "props": {"hover": True},
                            "content": [{"component": "tbody", "content": detail_rows}],
                        }],
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
                                "component": "div",
                                "props": {"class": "text-h6 mb-2"},
                                "text": "最近记录",
                            },
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
                                                {"component": "th", "text": "消息"},
                                            ],
                                        }],
                                    },
                                    {"component": "tbody", "content": history_rows},
                                ],
                            },
                        ],
                    }
                ],
            },
        ]

    @staticmethod
    def _build_table_rows(items: List[Tuple[str, Any]]) -> List[dict]:
        rows = []
        for label, value in items:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-subtitle-2 text-no-wrap"}, "text": label},
                    {"component": "td", "text": str(value) if value is not None else ""},
                ],
            })
        return rows
