# _*_ coding: utf-8 _*_
"""TranslateOpsMixin for SubtitleHunter — extracted for maintainability."""
from __future__ import annotations

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

import requests
from app.core.config import settings
from app.log import logger

from .codes import Stage
from . import formats as formats_mod
from . import quality as quality_mod
from .models import SubtitleCue


class TranslateOpsMixin:
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

    def _translation_profile_label(self) -> str:
        """Return a Chinese label for the current translation profile."""
        labels = {
            "fast": "快速",
            "standard": "标准",
            "quality": "质量优先",
        }
        label = labels.get(self._translation_profile, self._translation_profile)
        return f"{label}({self._translation_profile})"

