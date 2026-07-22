# _*_ coding: utf-8 _*_
"""RuntimeOpsMixin for SubtitleHunter — extracted for maintainability."""
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.context import MediaInfo
from app.log import logger
from app.schemas.types import NotificationType

from .codes import FailureCode, Stage, map_error_message


class RuntimeOpsMixin:
    def stop_service(self):
        """Signal any in-flight background job to stop at the next safe checkpoint."""
        self._ensure_runtime_status()
        with self._status_lock:
            cancel_event = getattr(self, "_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
            if self._job_active or self._runtime.get("running"):
                self._runtime["message"] = "正在停止任务…"
                logger.info(f"【{self.plugin_name}】已请求停止后台字幕任务")

    def get_status(self) -> Dict[str, Any]:
        return self._runtime_snapshot()

    def api_cancel(self) -> Dict[str, Any]:
        """Request cooperative cancellation of the active background job."""
        self._ensure_runtime_status()
        with self._status_lock:
            active = bool(self._job_active or self._runtime.get("running"))
            cancel_event = getattr(self, "_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
        if not active:
            return {"success": True, "message": "当前没有运行中的任务"}
        self._update_run(message="已请求取消，等待当前步骤结束…")
        return {"success": True, "message": "已请求取消当前任务"}

    def _start_background_job(
        self,
        source: str,
        target_path: str,
        mediainfo: Optional[MediaInfo],
    ) -> bool:
        targets = [self._resolve_target_path(item) for item in self._split_target_paths(target_path)]
        if not targets:
            logger.warning(f"【{self.plugin_name}】未指定媒体目录或视频路径，任务未启动")
            return False

        self._ensure_runtime_status()
        with self._status_lock:
            if self._job_active:
                logger.warning(f"【{self.plugin_name}】已有字幕任务正在运行，跳过：{source}")
                return False
            self._job_active = True
            self._cancel_event = threading.Event()

        if len(targets) > 1:
            title = f"{len(targets)} 个目标"
            job = self._ensure_multiple_targets_workflow
            args = (source, targets)
        else:
            target = targets[0]
            title = self._media_title(mediainfo, target)
            job = self._ensure_chinese_workflow
            args = (source, target, title, mediainfo)

        try:
            threading.Thread(
                target=self._run_reserved_job,
                args=(job, args),
                daemon=True,
                name=f"SubtitleHunter-{title}",
            ).start()
            return True
        except Exception:
            with self._status_lock:
                self._job_active = False
            raise

    def _run_reserved_job(self, job, args: Tuple[Any, ...]):
        try:
            job(*args)
        except self._JobCancelled:
            logger.info(f"【{self.plugin_name}】字幕任务已取消")
            self._update_run(
                running=False,
                status="已取消",
                message="任务已取消",
                error="",
            )
            self._save_runtime_state()
        except Exception as e:
            error = traceback.format_exc()
            logger.error(f"【{self.plugin_name}】字幕任务启动后异常退出：{e}\n{error}")
            self._update_run(
                running=False,
                status="失败",
                message=f"字幕任务异常退出：{e}",
                error=error,
            )
            self._save_runtime_state()
        finally:
            self._ensure_runtime_status()
            with self._status_lock:
                self._job_active = False

    class _JobCancelled(Exception):
        """Raised when a cooperative cancel checkpoint is hit."""

    def _is_cancelled(self) -> bool:
        self._ensure_runtime_status()
        cancel_event = getattr(self, "_cancel_event", None)
        return bool(cancel_event is not None and cancel_event.is_set())

    def _raise_if_cancelled(self):
        if self._is_cancelled():
            raise self._JobCancelled("任务已取消")

    def _stage_begin(self, stage: Any) -> float:
        name = stage.value if isinstance(stage, Stage) else str(stage)
        self._update_run(current_stage=name, message=f"阶段开始：{name}")
        return time.monotonic()

    def _stage_end(self, stage: Any, started_at: float, **extra):
        name = stage.value if isinstance(stage, Stage) else str(stage)
        elapsed = max(time.monotonic() - started_at, 0.0)
        self._ensure_runtime_status()
        with self._status_lock:
            timings = dict(self._runtime.get("stage_timings") or {})
            timings[name] = round(timings.get(name, 0.0) + elapsed, 3)
            self._runtime["stage_timings"] = timings
            self._runtime["current_stage"] = name
            if extra:
                self._runtime.update(extra)
        logger.info(f"【{self.plugin_name}】阶段完成 {name}，耗时 {elapsed:.2f}s")
        self._save_runtime_state()

    def _record_failure_code(self, message: str, code: Optional[str] = None):
        failure_code = code or map_error_message(message)
        self._update_run(failure_code=failure_code)
        return failure_code

    def _interruptible_sleep(self, seconds: float):
        """Sleep in short slices so cancel requests can stop retries quickly."""
        deadline = time.monotonic() + max(seconds, 0)
        while True:
            self._raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

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

    def _runtime_state_path(self) -> Path:
        return self.get_data_path() / "runtime_state.json"

    def _init_runtime_status(self):
        self._status_lock = threading.Lock()
        self._job_active = False
        self._cancel_event = threading.Event()
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
            "current_stage": "",
            "stage_timings": {},
            "failure_code": "",
        }
        self._history = []

    def _load_runtime_state(self):
        try:
            path = self._runtime_state_path()
            if not path.exists():
                return
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return
            if not isinstance(payload, dict):
                return

            runtime = payload.get("runtime")
            history = payload.get("history")
            if not isinstance(runtime, dict):
                runtime = {}
            if not isinstance(history, list):
                history = []

            fields = (
                "status", "source", "media", "target_path", "started_at", "finished_at",
                "duration", "videos", "subtitles", "processed", "skipped", "extracted",
                "translated", "renamed", "failed", "last_video", "message", "error",
                "errors", "details", "translation_batches_total", "translation_batches_done",
            )
            with self._status_lock:
                self._history = [dict(item) for item in history if isinstance(item, dict)][:10]
                self._runtime.update({key: runtime[key] for key in fields if key in runtime})
                self._runtime["running"] = False
                if self._runtime.get("status") == "运行中":
                    self._runtime["status"] = "已中断"
        except OSError as e:
            logger.warning(f"【{self.plugin_name}】读取运行状态失败：{e}")
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】加载运行状态失败：{e}")

    def _save_runtime_state(self):
        try:
            path = self._runtime_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._status_lock:
                payload = {
                    "runtime": dict(self._runtime),
                    "history": [dict(item) for item in self._history[:10]],
                }
            tmp_path = path.with_name(f"{path.name}.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】保存运行状态失败：{e}")

    def _ensure_runtime_status(self):
        if not hasattr(self, "_status_lock"):
            self._init_runtime_status()
            return
        if not hasattr(self, "_job_active"):
            self._job_active = False
        if not hasattr(self, "_cancel_event"):
            self._cancel_event = threading.Event()

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
                "current_stage": "",
                "stage_timings": {},
                "failure_code": "",
            })
        return started_at

    def _update_run(self, **kwargs):
        self._ensure_runtime_status()
        with self._status_lock:
            self._runtime.update(kwargs)

    def _finish_run(self, status: str, message: str, started_at: float, error: str = "", **kwargs):
        failure_code = kwargs.pop("failure_code", None)
        if status in {"失败", "部分失败", "已取消"} and not failure_code:
            failure_code = map_error_message(error or message)
        if failure_code:
            kwargs["failure_code"] = failure_code
        elif status in {"完成"}:
            kwargs.setdefault("failure_code", FailureCode.OK.value)
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
        self._save_runtime_state()

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

    def _build_skip_notify_text(self, reason: str, source: str = "定时任务") -> str:
        """Build a readable notification for skipped scheduled runs."""
        lines = [
            f"来源：{source}",
            "状态：已跳过",
            f"原因：{self._truncate_notify_text(reason, 160)}",
        ]
        if self._target_path:
            lines.append(f"目标：{self._compact_notify_path(self._target_path)}")
        lines.append(f"时间：{self._now_text()}")
        return "\n".join(lines)

    def _build_start_notify_text(
        self,
        source: str,
        display_name: str,
        target: Path,
        video_count: int,
        subtitle_count: int,
        eta_seconds: int,
        scan_errors: List[str],
    ) -> str:
        """Build a readable notification for workflow start."""
        lines = [
            f"媒体：{display_name}",
            f"来源：{source}",
            f"目标：{self._compact_notify_path(target)}",
            f"范围：{video_count} 个视频，{subtitle_count} 条字幕轨",
            f"预计耗时：{self._format_duration(eta_seconds)}",
            f"翻译配置：{self._translation_profile_label()}，并发 {self._parallel_batches}，批次 {self._batch_chars} 字",
            f"开始时间：{self._now_text()}",
        ]
        if scan_errors:
            lines.append(f"扫描提醒：{len(scan_errors)} 条")
            for error in scan_errors[:2]:
                lines.append(f"- {self._truncate_notify_text(error, 120)}")
        return "\n".join(lines)

    def _build_finish_notify_text(
        self,
        final_status: str,
        source: str,
        display_name: str,
        target: Path,
        summary: Dict[str, int],
        details: List[Dict[str, Any]],
        duration_seconds: float,
    ) -> str:
        """Build a readable notification for workflow completion."""
        lines = [
            f"媒体：{display_name}",
            f"来源：{source}",
            f"状态：{final_status}",
            f"耗时：{self._format_duration(max(1, ceil(duration_seconds)))}",
            f"目标：{self._compact_notify_path(target)}",
            (
                "结果："
                f"处理 {summary.get('processed', 0)}，"
                f"生成 {summary.get('translated', 0)}，"
                f"提取 {summary.get('extracted', 0)}，"
                f"重命名 {summary.get('renamed', 0)}，"
                f"跳过 {summary.get('skipped', 0)}，"
                f"失败 {summary.get('failed', 0)}"
            ),
        ]

        translated_files = self._collect_notify_files(details, "translated_files")
        if translated_files:
            lines.append("生成字幕：")
            lines.extend(f"- {self._compact_notify_path(path)}" for path in translated_files[:3])

        extracted_files = self._collect_notify_files(details, "extracted_files")
        if extracted_files:
            lines.append("提取字幕：")
            lines.extend(f"- {self._compact_notify_path(path)}" for path in extracted_files[:2])

        failed_details = [detail for detail in details if detail.get("status") == "失败"]
        if failed_details:
            lines.append("失败原因：")
            for detail in failed_details[:3]:
                video_name = Path(str(detail.get("video") or "未知视频")).name
                reason = self._truncate_notify_text(detail.get("message") or "未知错误", 160)
                lines.append(f"- {video_name}：{reason}")
        elif not translated_files and not extracted_files:
            lines.append("说明：未生成新字幕，通常是已有中文字幕或 AI 翻译不可用。")

        lines.append(f"完成时间：{self._now_text()}")
        return "\n".join(lines)

    def _build_failure_notify_text(
        self,
        source: str,
        display_name: str,
        target: Path,
        error: Exception,
    ) -> str:
        """Build a readable notification for unexpected workflow exceptions."""
        return "\n".join([
            f"媒体：{display_name}",
            f"来源：{source}",
            "状态：失败",
            f"目标：{self._compact_notify_path(target)}",
            f"错误：{self._truncate_notify_text(error, 220)}",
            f"时间：{self._now_text()}",
        ])

    def _collect_notify_files(self, details: List[Dict[str, Any]], field: str) -> List[str]:
        """Collect file paths from detail records for compact notifications."""
        files = []
        for detail in details:
            for item in detail.get(field, []) or []:
                files.append(str(item))
        return files

    def _compact_notify_path(self, path: Any, max_parts: int = 3) -> str:
        """Shorten long paths while keeping the most useful trailing parts."""
        text = str(path or "")
        if not text:
            return "-"
        parts = Path(text).parts
        if len(parts) <= max_parts:
            return text
        return ".../" + "/".join(parts[-max_parts:])

    def _truncate_notify_text(self, value: Any, limit: int) -> str:
        """Collapse whitespace and trim long notification fragments."""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return f"{text[:max(limit - 3, 0)]}..."

