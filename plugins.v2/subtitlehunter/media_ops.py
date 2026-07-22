# _*_ coding: utf-8 _*_
"""MediaOpsMixin for SubtitleHunter — extracted for maintainability."""
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger

from .models import SubtitleTrack


class MediaOpsMixin:
    def _is_usable_external_chinese_track(self, track: SubtitleTrack) -> bool:
        if track.forced or not track.path or not track.path.exists():
            return False
        if not track.text_based or track.path.suffix.lower() not in self._TRANSLATABLE_EXTS:
            try:
                return track.path.stat().st_size > 0
            except OSError:
                return False
        return self._subtitle_file_has_chinese_text(track.path)

    def _subtitle_file_has_chinese_text(self, path: Path) -> bool:
        try:
            content = path.read_text(encoding="utf-8-sig", errors="ignore")
            suffix = path.suffix.lower()
            if suffix == ".srt":
                cues = self._parse_srt(content)
            elif suffix in {".ass", ".ssa"}:
                _, cues = self._parse_ass(content)
            else:
                return False
            return self._has_chinese_cue_coverage(cues)
        except (OSError, UnicodeError):
            return False

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

