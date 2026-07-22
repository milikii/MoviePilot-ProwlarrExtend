# _*_ coding: utf-8 _*_
"""WorkflowMixin for SubtitleHunter — extracted for maintainability."""
from __future__ import annotations

import re
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.log import logger
from app.schemas.types import EventType

from . import eta as eta_mod
from . import language as language_mod
from .models import SubtitleTrack


class WorkflowMixin:
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

