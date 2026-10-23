# _*_ coding: utf-8 _*_
"""MediaOpsMixin for SubtitleHunter — extracted for maintainability."""
import json
import subprocess
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger

from .models import SubtitleTrack
from . import formats


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
            content, encoding = formats.decode_subtitle(path.read_bytes())
            logger.info(f"【{self.plugin_name}】读取字幕：{path}，编码 {encoding}")
            suffix = path.suffix.lower()
            if suffix == ".srt":
                cues = self._parse_srt(content)
                if cues.bad_blocks:
                    logger.warning(f"【{self.plugin_name}】字幕坏块 {cues.bad_blocks}：{cues.diagnostics}")
                    return False
            elif suffix in {".ass", ".ssa"}:
                _, cues = self._parse_ass(content)
            else:
                return False
            return self._has_chinese_cue_coverage(cues)
        except (OSError, UnicodeError, ValueError) as error:
            logger.warning(f"【{self.plugin_name}】字幕读取失败：{path}，{error}")
            return False

    def _scan_target(self, target: Path) -> Dict[str, Any]:
        result = {"videos": [], "subtitles": [], "errors": [], "video_tracks": {}}
        if not target:
            result["errors"].append("未指定目标路径")
            return result

        videos = self._find_videos(target)
        result["videos"] = videos

        for video in videos:
            external_tracks = self._find_external_subtitles(video)
            try:
                embedded_tracks = self._probe_embedded_subtitles(video)
                result["video_tracks"][str(video)] = {
                    "external": external_tracks,
                    "embedded": embedded_tracks,
                }
            except Exception as e:
                embedded_tracks = []
                result["errors"].append(f"{video}: {e}")
            result["subtitles"].extend(external_tracks)
            result["subtitles"].extend(embedded_tracks)

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
            forced=self._forced_modifier(subtitle_path.stem, video_path.stem if video_path else ""),
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
                forced=bool(disposition.get("forced")) or self._forced_modifier(title, video_path.stem),
                default=bool(disposition.get("default")),
                text_based=codec in self._TEXT_CODECS,
                extension=extension,
            ))
        return tracks

    def _forced_modifier(self, title: str, video_stem: str = "") -> bool:
        """Only subtitle suffixes or standalone metadata modifiers are evidence."""
        import re
        if (video_stem and title.lower().startswith(video_stem.lower())
                and (len(title) == len(video_stem) or title[len(video_stem)] in "._- [()]")):
            title = title[len(video_stem):]
        else:
            # Without a known movie prefix, consider only trailing modifier segments.
            parts = [part for part in re.split(r"[._\-\s\[\]()]", title) if part]
            suffix = []
            for part in reversed(parts):
                if (self._FORCED_PATTERNS.fullmatch(part) or self._ENG_PATTERNS.fullmatch(part)
                        or self._CHI_PATTERNS.fullmatch(part)
                        or part.lower() in {"sdh", "cc", "ai", "hans", "hant", "cn", "tw"}
                        or re.fullmatch(r"stream\d+", part, re.I)):
                    suffix.append(part)
                else:
                    break
            title = ".".join(reversed(suffix))
        return bool(self._FORCED_PATTERNS.search(title))

    def _extract_embedded_subtitle(self, track: SubtitleTrack) -> Tuple[bool, Optional[Path], str]:
        if not track.video_path or track.stream_index is None:
            return False, None, "缺少视频路径或字幕流索引"
        if not track.text_based:
            return False, None, "图形字幕无法直接提取为 srt/ass"

        output = self._extracted_subtitle_path(track)
        if output.exists() and not self._overwrite:
            return True, output, f"字幕已存在：{output}"

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=f".{output.stem}.", suffix=output.suffix,
                                         dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            self._raise_if_cancelled()
            ok, stdout, stderr = self._run_command([
                "ffmpeg", "-nostdin", "-y", "-i", str(track.video_path),
                "-map", f"0:{track.stream_index}", str(temporary),
            ], timeout=self._ffmpeg_timeout)
            if not ok:
                return False, output, f"ffmpeg 提取失败：{stderr or stdout}"
            content, _ = formats.decode_subtitle(temporary.read_bytes())
            if output.suffix.lower() == ".srt":
                cues = self._parse_srt(content)
                if cues.bad_blocks:
                    raise ValueError(f"提取字幕坏块：{cues.diagnostics}")
            else:
                _, cues = self._parse_ass(content)
            if not cues:
                raise ValueError("提取字幕无有效条目")
            self._raise_if_cancelled()
            os.replace(temporary, output)
            logger.info(f"【{self.plugin_name}】已提取字幕：{track.video_path} stream {track.stream_index} -> {output}")
            return True, output, f"提取成功：{output}"
        except self._JobCancelled:
            raise
        except (OSError, ValueError) as error:
            return False, output, f"ffmpeg 提取失败：{error}"
        finally:
            temporary.unlink(missing_ok=True)

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

