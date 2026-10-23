# _*_ coding: utf-8 _*_
"""TranslateOpsMixin for SubtitleHunter — extracted for maintainability."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from email.utils import parsedate_to_datetime
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
from . import batching as batching_mod
from . import formats as formats_mod
from . import linecheck as linecheck_mod
from . import quality as quality_mod
from .models import SubtitleCue


class AIRequestError(RuntimeError):
    def __init__(self, message, status_code=None, retryable=False, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


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
        diagnostics = {"source": str(source_path)}
        try:
            self._raise_if_cancelled()
            content, encoding = formats_mod.decode_subtitle(source_path.read_bytes())
            diagnostics = {"source": str(source_path), "encoding": encoding,
                           "bad_blocks": 0, "parse_diagnostics": []}
            logger.info(f"【{self.plugin_name}】字幕解码：{source_path}，编码 {encoding}")
            if suffix == ".srt":
                cues = self._parse_srt(content)
                renderer = self._render_srt
                diagnostics.update(bad_blocks=cues.bad_blocks, parse_diagnostics=cues.diagnostics)
                self._update_run(subtitle_diagnostics=diagnostics)
                if cues.bad_blocks:
                    logger.error(f"【{self.plugin_name}】SRT 坏块 {cues.bad_blocks}：{cues.diagnostics}")
                    return False, f"字幕解析失败：编码 {encoding}，坏块 {cues.bad_blocks}：{cues.diagnostics}"
            elif suffix in {".ass", ".ssa"}:
                lines, cues = self._parse_ass(content)

                def renderer(items, _lines=lines):
                    return self._render_ass(_lines, items)
            else:
                return False, f"暂不支持翻译 {suffix} 字幕"
            if not cues:
                return False, f"字幕翻译失败：编码 {encoding}，未解析到有效字幕条目"
            counts = {}
            for cue in cues:
                formats_mod.tag_layout(cue.text)
                reason = quality_mod.no_translation_reason(cue.text)
                counts[reason or "needs_translation"] = counts.get(reason or "needs_translation", 0) + 1
                cue.no_translation_reason = reason
            diagnostics["classification_counts"] = counts
            self._update_run(subtitle_diagnostics=diagnostics)
            translated = self._translate_cues(cues, media_context, ai_config)
            valid, reason = self._validate_translated_cues(cues, translated)
            if not valid:
                diagnostics["failure"] = reason
                self._update_run(subtitle_diagnostics=diagnostics)
                return False, f"字幕翻译失败：{reason}"
            self._raise_if_cancelled()
            rendered = renderer(translated)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             prefix=f".{output_path.name}.", suffix=output_path.suffix,
                                             dir=output_path.parent, delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            # Reparse the actual artifact before publication, including tags and timestamps.
            artifact, _ = formats_mod.decode_subtitle(temp_path.read_bytes())
            if suffix == ".srt":
                check = self._parse_srt(artifact)
                if check.bad_blocks:
                    raise ValueError(f"成品解析坏块：{check.diagnostics}")
            else:
                _, check = self._parse_ass(artifact)
            if len(check) != len(translated) or any(
                    (a.start, a.end, a.text, a.time_tail) != (b.start, b.end, b.text, b.time_tail)
                    for a, b in zip(translated, check)):
                raise ValueError("成品全量回读校验失败")
            self._raise_if_cancelled()
            os.replace(temp_path, output_path)
            temp_path = None
            logger.info(f"【{self.plugin_name}】字幕翻译完成：{source_path} -> {output_path}")
            return True, f"翻译完成：{output_path}；编码 {encoding}；分类计数 {counts}"
        except self._JobCancelled:
            raise
        except Exception as e:
            diagnostics["failure"] = str(e)
            self._update_run(subtitle_diagnostics=diagnostics)
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
        translated_by_id: Dict[int, str] = {}
        assigned = [cue.cue_id for cue in cues if cue.cue_id is not None]
        if len(set(assigned)) != len(assigned):
            raise ValueError("源 cue ID 重复")
        next_id = max(assigned, default=0) + 1
        for cue_id, cue in enumerate(cues, start=1):
            if cue.cue_id is None:
                cue.cue_id = next_id
                next_id += 1
            cue.no_translation_reason = quality_mod.no_translation_reason(cue.text)
            formats_mod.tag_layout(cue.text)
        translatable = [cue for cue in cues if not cue.no_translation_reason]

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
                            translated_by_id[cue.cue_id] = result.get(cue.cue_id, "")
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
                if cue.no_translation_reason:
                    output.append(cue)
                else:
                    text = translated_by_id.get(cue.cue_id, "")
                    try:
                        text = formats_mod.restore_tags(cue.text, text) if text else ""
                    except formats_mod.TagError as error:
                        logger.error(f"【{self.plugin_name}】cue ID {cue.cue_id} 标签恢复失败：{error}")
                        text = ""
                    output.append(replace(cue, text=text))
            valid, reason = self._validate_translated_cues(cues, output)
            if not valid:
                raise ValueError(reason)
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
            {"index": cue.cue_id, "text": formats_mod.mask_tags(cue.text), "tag_source": cue.text}
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
                cue.cue_id: polished.get(cue.cue_id, "")
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
            cue.cue_id: final.get(cue.cue_id, "")
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
            return self._validate_stage_result(items, cached)

        payload = {
            "items": items,
            "previous": [
                {"index": index, "text": text}
                for index, text in sorted((previous or {}).items())
            ],
        }
        prompt = self._translation_prompt(stage, media_context, glossary_text)

        def parse(content):
            parsed = self._parse_translation_response(content)
            return self._validate_stage_result(items, parsed)

        parsed = self._request_stage(stage_name, [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], ai_config, parse)
        self._save_translation_cache(cache_key, parsed, ai_config)
        return parsed

    def _validate_stage_result(self, items, parsed):
        requested = {int(item["index"]) for item in items}
        missing = requested - set(parsed)
        missing.update(item["index"] for item in items if not parsed.get(item["index"], "").strip())
        if missing:
            raise ValueError(quality_mod.coverage_failure(missing))
        if set(parsed) != requested:
            raise ValueError("响应包含未知 cue ID")
        for item in items:
            if not isinstance(parsed[item["index"]], str):
                raise ValueError("译文不是字符串")
            source = item.get("tag_source", item["text"])
            try:
                restored = formats_mod.restore_tags(source, parsed[item["index"]])
                parsed[item["index"]] = formats_mod.mask_tags(restored)
            except formats_mod.TagError as error:
                raise ValueError(quality_mod.coverage_failure([item["index"]], str(error))) from error
        return parsed

    def _request_stage(self, stage_name, messages, ai_config, parse):
        """One deadline covers all requests, response parsing and retry waits."""
        deadline = time.monotonic() + self._api_stage_budget
        last_error = None
        for attempt in range(self._api_retries + 1):
            self._raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                config = dict(ai_config, _deadline=deadline)
                content = self._chat_completion(messages, config)
                self._raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("阶段总预算耗尽")
                parsed = parse(content)
                if time.monotonic() >= deadline:
                    raise TimeoutError("阶段总预算耗尽")
                return parsed
            except (MemoryError, self._JobCancelled):
                raise
            except Exception as error:
                last_error = error
                if isinstance(error, AIRequestError) and not error.retryable:
                    raise RuntimeError(f"{stage_name}失败：{error}") from error
                if not isinstance(error, (AIRequestError, requests.RequestException, TimeoutError)):
                    raise RuntimeError(f"{stage_name}失败：{error}") from error
                if attempt >= self._api_retries:
                    break
                delay = min(30.0, 2.0 * 2 ** min(attempt, 4))
                if isinstance(error, AIRequestError) and error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or delay >= remaining:
                    break
                logger.warning(f"【{self.plugin_name}】{stage_name}重试等待 {delay}s：{error}")
                self._interruptible_sleep(delay)
        raise RuntimeError(f"{stage_name}失败，阶段总预算 {self._api_stage_budget}s/"
                           f"重试上限 {self._api_retries}：{last_error}")

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
            "version": 6,
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
            "index 是内部唯一 ID，不是文件印刷序号。[[SH_TAG_000]] 等标签占位符必须原样保留且顺序不变。"
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
        self._raise_if_cancelled()
        deadline = ai_config.get("_deadline", time.monotonic() + self._api_stage_budget)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("阶段总预算耗尽")
        proxies = getattr(settings, "PROXY", None) if ai_config.get("use_proxy") else {"http": None, "https": None}
        try:
            response = requests.post(url, headers=headers, json=body,
                                     timeout=min(self._api_timeout, remaining), proxies=proxies)
        except requests.RequestException as error:
            raise AIRequestError(f"AI API 网络错误：{error}", retryable=True) from error
        try:
            status = response.status_code
            if status >= 400:
                retry_after = None
                if status == 429:
                    value = response.headers.get("Retry-After", "")
                    try:
                        retry_after = max(0.0, float(value))
                    except ValueError:
                        try:
                            retry_after = max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            pass
                raise AIRequestError(f"AI API HTTP {status}: {response.text[:500]}",
                                     status_code=status, retryable=status == 429 or 500 <= status < 600,
                                     retry_after=retry_after)
            try:
                payload = response.json()
                return payload["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise AIRequestError(f"AI API HTTP {status} 返回格式不兼容：{response.text[:500]}") from error
        finally:
            response.close()

    def _parse_translation_response(self, content: str) -> Dict[int, str]:
        return quality_mod.parse_translation_response(content)

    def _chunk_cues(self, cues: List[SubtitleCue]) -> List[List[SubtitleCue]]:
        return batching_mod.chunk_cues(
            cues,
            self._batch_size,
            self._batch_chars,
            text_fn=lambda cue: cue.text,
            is_sentence_boundary=self._is_sentence_boundary,
        )

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
            except self._JobCancelled:
                raise
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
            {"index": cue.cue_id, "text": formats_mod.mask_tags(cue.text)}
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
        parsed = self._request_stage("术语抽取", [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], ai_config, self._parse_glossary_response)
        self._save_glossary_cache(cache_key, parsed, ai_config)
        return parsed

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
        return quality_mod.parse_user_glossary(self._glossary or "")

    @staticmethod
    def _glossary_key(term: str) -> str:
        """Normalize glossary terms for duplicate detection and user override matching."""
        return quality_mod.glossary_key(term)

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
            "[[SH_TAG_000]] 等标签占位符必须原样保留且顺序不变，不计入长度限制。"
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
            if violations and not cue.no_translation_reason:
                issues.append((cue, violations))
        if not issues:
            return cues

        logger.info(f"【{self.plugin_name}】字幕长度校验发现超标条目：{len(issues)} 条，开始压缩")
        compressed = self._compress_line_length_issues(issues, media_context, ai_config, glossary_text)
        if not compressed:
            return cues

        output = []
        for cue in cues:
            text = compressed.get(cue.cue_id)
            if text:
                try:
                    text = formats_mod.restore_tags(cue.text, text)
                except formats_mod.TagError as error:
                    raise ValueError(quality_mod.coverage_failure([cue.cue_id], str(error))) from error
                output.append(replace(cue, text=text))
            else:
                output.append(cue)
        return output

    def _line_length_violations(self, cue: SubtitleCue) -> List[str]:
        """Return Netflix-style line count, line length, and CPS violations for one cue."""
        return linecheck_mod.line_length_violations(cue)

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
                "index": cue.cue_id,
                "text": formats_mod.mask_tags(cue.text),
                "tag_source": cue.text,
                "violations": "；".join(violations),
                "duration_seconds": round(duration, 3) if duration > 0 else 0,
                "limits": "中文每行<=16；英文每行<=42；每条<=2行；CPS<=15",
            })
        return self._translate_stage(
            stage="compress", items=items, media_context=media_context,
            previous=None, ai_config=ai_config, glossary_text=glossary_text,
        )

    def _cue_duration_seconds(self, cue: SubtitleCue) -> float:
        """Calculate cue duration in seconds from SRT or ASS timestamp strings."""
        return linecheck_mod.cue_duration_seconds(cue)

    @staticmethod
    def _parse_subtitle_time(value: str) -> Optional[float]:
        """Parse SRT/ASS timestamps such as 00:01:02,345 or 0:01:02.34."""
        return linecheck_mod.parse_subtitle_time(value)

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

