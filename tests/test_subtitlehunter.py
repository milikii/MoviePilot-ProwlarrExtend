import threading
import time

import pytest


VALID_ENGLISH_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello world.

2
00:00:04,000 --> 00:00:06,000
How are you?
"""


def _plugin(plugin_modules, tmp_path):
    plugin = plugin_modules.subtitle.SubtitleHunter()
    plugin.get_data_path = lambda *args, **kwargs: tmp_path / "data"
    plugin.init_plugin({})
    plugin._notify = False
    return plugin


def test_background_job_admission_is_atomic(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    started = []
    first_started = threading.Event()
    release = threading.Event()

    def worker(*args):
        started.append(threading.get_ident())
        first_started.set()
        release.wait(2)

    plugin._ensure_chinese_workflow = worker
    try:
        first = plugin._start_background_job("test", str(tmp_path / "movie.mkv"), None)
        assert first_started.wait(1)
        second = plugin._start_background_job("test", str(tmp_path / "movie.mkv"), None)
        time.sleep(0.05)
    finally:
        release.set()

    assert first is True
    assert second is False
    assert len(started) == 1


def test_forced_chinese_does_not_satisfy_full_subtitle_requirement(plugin_modules, tmp_path):
    module = plugin_modules.subtitle
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._rename_existing = False
    video = tmp_path / "movie.mkv"
    english = tmp_path / "movie.en.srt"
    english.write_text(VALID_ENGLISH_SRT, encoding="utf-8")
    forced_path = tmp_path / "movie.zh-Hans.forced.srt"
    forced_path.write_text(
        VALID_ENGLISH_SRT.replace("Hello world.", "你好，世界。").replace("How are you?", "你好吗？"),
        encoding="utf-8",
    )
    forced = module.SubtitleTrack(
        source="external",
        path=forced_path,
        video_path=video,
        stream_index=None,
        codec="srt",
        language="zh-Hans",
        title="Chinese forced",
        forced=True,
        default=False,
        text_based=True,
        extension=".srt",
    )
    plugin._find_external_subtitles = lambda _video: [forced]
    plugin._probe_embedded_subtitles = lambda _video: []
    plugin._select_english_source = lambda *args: english
    plugin._resolve_ai_config = lambda: ({"model": "test"}, "")
    plugin._translate_subtitle_file = lambda **kwargs: (True, "translated")

    result = plugin._ensure_video_chinese(video, "context")

    assert result["status"] == "已生成中文"


def test_invalid_named_chinese_file_is_not_treated_as_success(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._rename_existing = False
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / "movie.zh-Hans.ai.srt").write_text("not a subtitle", encoding="utf-8")
    english = tmp_path / "movie.en.srt"
    english.write_text(VALID_ENGLISH_SRT, encoding="utf-8")
    plugin._probe_embedded_subtitles = lambda _video: []
    plugin._resolve_ai_config = lambda: ({"model": "test"}, "")
    calls = []

    def translate(**kwargs):
        calls.append(kwargs)
        return True, "translated"

    plugin._translate_subtitle_file = translate
    result = plugin._ensure_video_chinese(video, "context")

    assert result["status"] == "已生成中文"
    assert len(calls) == 1


def test_malformed_source_is_not_written_as_success(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    source = tmp_path / "movie.en.srt"
    output = tmp_path / "movie.zh-Hans.ai.srt"
    source.write_text("this is not a valid subtitle", encoding="utf-8")

    ok, message = plugin._translate_subtitle_file(source, output, "context", {})

    assert ok is False
    assert "解析" in message or "有效" in message
    assert not output.exists()


def test_pure_english_model_output_fails_quality_gate(plugin_modules, tmp_path):
    module = plugin_modules.subtitle
    plugin = _plugin(plugin_modules, tmp_path)
    source = tmp_path / "movie.en.srt"
    output = tmp_path / "movie.zh-Hans.ai.srt"
    source.write_text(VALID_ENGLISH_SRT, encoding="utf-8")
    plugin._translate_cues = lambda cues, *_: [
        module.SubtitleCue(cue.index, cue.start, cue.end, cue.text)
        for cue in cues
    ]

    ok, _message = plugin._translate_subtitle_file(source, output, "context", {})

    assert ok is False
    assert not output.exists()


def test_valid_chinese_translation_is_atomically_written(plugin_modules, tmp_path):
    module = plugin_modules.subtitle
    plugin = _plugin(plugin_modules, tmp_path)
    source = tmp_path / "movie.en.srt"
    output = tmp_path / "movie.zh-Hans.ai.srt"
    source.write_text(VALID_ENGLISH_SRT, encoding="utf-8")
    translations = ["你好，世界。", "你好吗？"]
    plugin._translate_cues = lambda cues, *_: [
        module.SubtitleCue(cue.index, cue.start, cue.end, translations[index])
        for index, cue in enumerate(cues)
    ]

    ok, _message = plugin._translate_subtitle_file(source, output, "context", {})

    assert ok is True
    assert "你好" in output.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp*"))


def test_translation_retry_is_bounded(plugin_modules, tmp_path, monkeypatch):
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._api_retries = 0
    plugin._api_timeout = 1
    plugin._cache_enabled = False
    calls = []

    def completion(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("offline")
        return '[{"index": 1, "text": "后来成功"}]'

    plugin._chat_completion = completion
    monkeypatch.setattr(plugin_modules.subtitle.time, "sleep", lambda _delay: None)
    with pytest.raises(RuntimeError, match="失败"):
        plugin._translate_stage(
            stage="direct",
            items=[{"index": 1, "text": "Hello"}],
            media_context="context",
            previous=None,
            ai_config={"source": "custom", "model": "test", "base_url": "https://example.invalid"},
        )

    assert len(calls) == 1


def test_glossary_retry_is_bounded(plugin_modules, tmp_path, monkeypatch):
    module = plugin_modules.subtitle
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._api_retries = 0
    plugin._api_timeout = 1
    plugin._cache_enabled = False
    calls = []

    def completion(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("offline")

    plugin._chat_completion = completion
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    cue = module.SubtitleCue(1, "00:00:01,000", "00:00:03,000", "Hello")

    with pytest.raises(RuntimeError, match="术语抽取失败"):
        plugin._run_glossary_stage(1, 1, [cue], "context", {"model": "test"})

    assert len(calls) == 1


def test_eta_does_not_multiply_total_chars_by_video_count(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._translation_profile = "fast"
    plugin._batch_chars = 9000
    plugin._parallel_batches = 1

    one_video = plugin._estimate_eta_seconds(1, 9000)
    ten_videos = plugin._estimate_eta_seconds(10, 9000)

    assert one_video > 0
    assert ten_videos == one_video


def test_translation_cache_cleanup_respects_max_files(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._CACHE_MAX_FILES = 3
    plugin._CACHE_MAX_AGE_DAYS = 30
    cache_dir = plugin.get_data_path() / "translation_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(5):
        path = cache_dir / f"{index}.json"
        path.write_text("{}", encoding="utf-8")
        paths.append(path)
        time.sleep(0.01)

    plugin._cleanup_translation_cache()

    remaining = sorted(cache_dir.glob("*.json"))
    assert len(remaining) == 3
    assert {path.name for path in remaining} == {"2.json", "3.json", "4.json"}


def test_cancel_stops_interruptible_sleep(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._cancel_event = threading.Event()
    plugin._cancel_event.set()

    with pytest.raises(plugin._JobCancelled):
        plugin._interruptible_sleep(5)


def test_api_cancel_without_running_job(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    result = plugin.api_cancel()
    assert result["success"] is True
    assert "没有运行中" in result["message"]


def test_ensure_video_chinese_with_fake_translation(plugin_modules, tmp_path):
    module = plugin_modules.subtitle
    plugin = _plugin(plugin_modules, tmp_path)
    plugin._rename_existing = False
    plugin._ai_enabled = True
    video = tmp_path / "movie.mkv"
    video.touch()
    english = tmp_path / "movie.en.srt"
    english.write_text(VALID_ENGLISH_SRT, encoding="utf-8")
    plugin._probe_embedded_subtitles = lambda _video: []
    plugin._resolve_ai_config = lambda: ({"model": "test", "source": "custom"}, "")
    translations = {"Hello world.": "你好，世界。", "How are you?": "你好吗？"}

    def translate_cues(cues, *_args):
        return [
            module.SubtitleCue(
                cue.index,
                cue.start,
                cue.end,
                translations.get(cue.text, f"译:{cue.text}"),
            )
            for cue in cues
        ]

    plugin._translate_cues = translate_cues
    result = plugin._ensure_video_chinese(video, "Example Movie")

    output = tmp_path / "movie.zh-Hans.ai.srt"
    assert result["status"] == "已生成中文"
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "你好，世界。" in content
    assert "你好吗？" in content
    assert str(output) in result["translated_files"]


def test_runtime_state_survives_reload(plugin_modules, tmp_path):
    plugin = _plugin(plugin_modules, tmp_path)
    started_at = plugin._start_run("手动", tmp_path / "movie.mkv", "Example")
    plugin._finish_run("完成", "处理完成", started_at, processed=1, translated=1)

    restored = plugin_modules.subtitle.SubtitleHunter()
    restored.get_data_path = lambda *args, **kwargs: tmp_path / "data"
    restored.init_plugin({})

    assert restored._runtime["status"] == "完成"
    assert restored._runtime["message"] == "处理完成"
    assert restored._runtime["running"] is False
    assert restored._history
    assert restored._history[0]["status"] == "完成"
    assert restored._history[0]["message"] == "处理完成"


def test_split_target_paths_supports_comma_variants(plugin_modules):
    split = plugin_modules.subtitle.SubtitleHunter._split_target_paths
    assert split("/a,/b，/c") == ["/a", "/b", "/c"]
    assert split("  ") == []
