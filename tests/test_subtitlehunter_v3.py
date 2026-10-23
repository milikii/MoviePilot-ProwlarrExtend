"""Isolated host substitutes and fake HTTP/clock regressions, not host integration tests.

The legacy adapter executes the unchanged pytest-style v2 functions under unittest;
its fixture/raises substitute is local to those imports and implements only used APIs.
"""
import codecs
import importlib.util
import inspect
import json
import re
import sys
import tempfile
import threading
import types
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path, package=False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def pytest_substitute():
    substitute = types.ModuleType("pytest")
    substitute.fixture = lambda **kwargs: lambda function: function

    @contextmanager
    def raises(error_type, match=None):
        try:
            yield
        except error_type as error:
            if match is not None and not re.search(match, str(error)):
                raise AssertionError(f"{error!r} does not match {match!r}") from error
        else:
            raise AssertionError(f"Expected {error_type.__name__}")

    substitute.raises = raises
    return substitute


class SubtitleHunterV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.stack.enter_context(patch.dict(sys.modules, {"pytest": pytest_substitute()}))
        original_path = list(sys.path)
        cls.addClassCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        stubs = load_file("_subtitle_v3_stubs", ROOT / "tests/conftest.py")
        stubs._install_import_stubs()
        cls.module = load_file("_subtitlehunter_v3_test", ROOT / "plugins.v3/subtitlehunter/__init__.py", True)
        cls.formats = sys.modules[cls.module.__name__ + ".formats"]
        cls.quality = sys.modules[cls.module.__name__ + ".quality"]
        cls.ops = sys.modules[cls.module.__name__ + ".translate_ops"]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.plugin = self.module.SubtitleHunter()
        self.plugin.get_data_path = lambda *args, **kwargs: self.directory / "data"
        self.plugin.init_plugin({"api_retries": 0, "translation_profile": "fast", "cache_enabled": False})
        self.plugin._ai_enabled = True
        self.plugin._generate_ai_glossary = lambda *args: {}
        self.plugin._enable_line_check = False
        self.source = self.directory / "movie.en.srt"
        self.output = self.directory / "movie.zh-Hans.ai.srt"
        self.config = {"base_url": "https://example.invalid/v1", "api_key": "secret", "model": "test"}
        self.calls = []
        self.responses = []
        self.clock = 0.0
        self.delays = []

    @staticmethod
    def srt(*texts, duplicate=False):
        return "\n\n".join(f"{1 if duplicate else number}\n00:00:{number:02},000 --> 00:00:{number+1:02},000\n{text}"
                           for number, text in enumerate(texts, 1)) + "\n"

    @staticmethod
    def response(content=None, status=200, headers=None):
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers or {})
        payload = {"choices": [{"message": {"content": content}}]} if status == 200 else {"error": "upstream rejected"}
        response._content = json.dumps(payload).encode()
        response.encoding = "utf-8"
        return response

    def transport(self, url, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def chinese_model(self, messages, config):
        payload = json.loads(messages[1]["content"])
        result = []
        for item in payload["items"]:
            text = item["text"]
            for english, chinese in (("First", "第一句中文"), ("Second", "第二句中文"), ("Hello", "你好世界"),
                                     ("World", "世界")):
                text = text.replace(english, chinese)
            result.append({"index": item["index"], "text": text})
        return json.dumps(result, ensure_ascii=False)

    def translate(self, content):
        self.source.write_text(content, encoding="utf-8")
        return self.plugin._translate_subtitle_file(self.source, self.output, "movie", self.config)

    def stage(self, name="direct"):
        return self.plugin._translate_stage(name, [{"index": 1, "text": "Hello"}], "movie", None, self.config)

    def fake_sleep(self, delay):
        self.plugin._raise_if_cancelled()
        self.delays.append(delay)
        self.clock += delay

    def fake_clock(self):
        stack = ExitStack()
        stack.enter_context(patch.object(self.ops.time, "monotonic", lambda: self.clock))
        stack.enter_context(patch.object(self.plugin, "_interruptible_sleep", self.fake_sleep))
        stack.enter_context(patch.object(self.ops.requests, "post", self.transport))
        return stack

    def test_srt_time_variants_normalize_and_tail_survives(self):
        for time_line, expected in (("0:00:01,00-->0:00:02,00", "00:00:01,000"),
                                    ("00:00:01.5 --> 00:00:02.5", "00:00:01,500"),
                                    ("0:0:1 --> 0:0:2", "00:00:01,000")):
            with self.subTest(time_line=time_line):
                cues = self.formats.parse_srt("1\n" + time_line + "\nHello")
                self.assertEqual(cues.bad_blocks, 0)
                self.assertEqual(cues[0].start, expected)
        cues = self.formats.parse_srt("1\n00:00:01,000 --> 00:00:02,000 X1:10 X2:20 Y1:30 Y2:40\nHello")
        self.assertIn("X1:10 X2:20 Y1:30 Y2:40", self.formats.render_srt(cues))

    def test_srt_bad_blocks_diagnosed_and_stop_whole_file(self):
        content = self.srt("Hello") + "\n2\n00:77:01,000 --> 00:77:02,000\nWorld\n\n3\nbroken axis\nLost\n"
        cues = self.formats.parse_srt(content)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues.bad_blocks, 2)
        self.assertEqual([item["block"] for item in cues.diagnostics], [2, 3])
        self.assertIn("Lost", cues.diagnostics[1]["snippet"])
        self.output.write_bytes(b"existing output")
        ok, message = self.translate(content)
        self.assertFalse(ok)
        self.assertIn("坏块 2", message)
        self.assertEqual(self.source.read_text(), content)
        self.assertEqual(self.output.read_bytes(), b"existing output")
        self.assertEqual(self.plugin.get_status()["subtitle_diagnostics"]["bad_blocks"], 2)

    def test_srt_invalid_fraction_and_tail_are_diagnosed(self):
        for axis in ("00:00:01,000 --> 00:00:02,0000", "00:00:03,000 --> 00:00:02,000",
                     "00:00:01,000 --> 00:00:02,000 unexpected"):
            with self.subTest(axis=axis):
                self.assertEqual(self.formats.parse_srt("1\n" + axis + "\nHello").bad_blocks, 1)

    def test_strict_decoding_bom_utf8_gbk_big5(self):
        for encoding in ("utf-8-sig", "utf-16", "utf-32", "gbk", "big5"):
            content = self.srt("中文音效")
            decoded, used = self.formats.decode_subtitle(content.encode(encoding))
            self.assertEqual(decoded, content)
            # Some Big5 sequences are valid GBK: strict candidate ordering is intentional.
            self.assertIn(used, (encoding, "gbk"))
        self.assertEqual(self.formats.decode_subtitle(b"Hello")[1], "utf-8")
        self.assertEqual(self.formats.decode_subtitle("你好".encode("gbk"))[1], "gbk")

    def test_undecodable_source_fails_and_preserves_bytes(self):
        for raw in (b"\xff", codecs.BOM_UTF8 + b"\xff", codecs.BOM_UTF16_LE + b"\x00"):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                self.output.write_bytes(b"old")
                ok, message = self.plugin._translate_subtitle_file(self.source, self.output, "movie", self.config)
                self.assertFalse(ok)
                self.assertEqual(self.source.read_bytes(), raw)
                self.assertEqual(self.output.read_bytes(), b"old")

    def test_duplicate_printed_indexes_have_independent_translations(self):
        cues = self.formats.parse_srt(self.srt("First", "Second", duplicate=True))
        self.assertEqual([cue.index for cue in cues], [1, 1])
        self.assertEqual([cue.cue_id for cue in cues], [1, 2])
        self.plugin._chat_completion = self.chinese_model
        translated = self.plugin._translate_cues(cues, "movie", self.config)
        self.assertEqual([cue.text for cue in translated], ["第一句中文", "第二句中文"])
        self.assertTrue(self.quality.validate_translated_cues(cues, translated)[0])

    def test_cache_key_includes_unique_id_and_revalidates_cached_coverage(self):
        self.plugin._cache_enabled = True
        items = [{"index": 1, "text": "Hello"}, {"index": 2, "text": "World"}]
        key = self.plugin._translation_cache_key("direct", items, "movie", None, self.config)
        changed = self.plugin._translation_cache_key("direct", [{"index": 9, "text": "Hello"}, items[1]], "movie", None, self.config)
        self.assertNotEqual(key, changed)
        self.plugin._save_translation_cache(key, {1: "你好"}, self.config)
        with self.assertRaisesRegex(ValueError, "缺失 1 条.*2"):
            self.plugin._translate_stage("direct", items, "movie", None, self.config)

    def test_missing_translation_fails_atomically_with_count_and_id(self):
        self.plugin._chat_completion = lambda *args: '[{"index":1,"text":"你好世界"}]'
        content = self.srt("Hello", "World")
        self.output.write_bytes(b"old subtitle")
        ok, message = self.translate(content)
        self.assertFalse(ok)
        self.assertIn("缺失 1 条", message)
        self.assertIn("2", message)
        self.assertEqual(self.output.read_bytes(), b"old subtitle")
        self.assertEqual(self.source.read_bytes(), content.encode())

    def test_half_chinese_and_unchanged_dialogue_fail_per_cue(self):
        cues = self.formats.parse_srt(self.srt("First", "Second"))
        translated = [replace(cues[0], text="第一句中文"), replace(cues[1], text="Second")]
        ok, message = self.quality.validate_translated_cues(cues, translated)
        self.assertFalse(ok)
        self.assertIn("缺失 1 条", message)

    def test_explicit_exemptions_counted_and_preserved(self):
        self.plugin._chat_completion = self.chinese_model
        ok, message = self.translate(self.srt("Hello", "[music]", "♪", "", "中文音效"))
        self.assertTrue(ok, message)
        counts = self.plugin.get_status()["subtitle_diagnostics"]["classification_counts"]
        self.assertEqual(counts, {"needs_translation": 1, "sound_effect": 1,
                                  "symbols_or_music": 1, "empty": 1, "already_chinese": 1})
        self.assertIn("[music]", self.output.read_text())
        self.assertFalse(self.quality.no_translation_reason("[Hello World]"))
        self.assertFalse(self.quality.no_translation_reason("♪ Hello World ♪"))

    def test_forced_filename_modifiers_and_movie_titles(self):
        for stem in ("The.Only.Living.Boy.2018", "Signs.2002", "Foreign.Correspondent.1940", "Only.The.Brave.2017", "A.forced.Movie"):
            video = self.directory / (stem + ".mkv")
            for suffix, expected in ((".en.srt", False), (".en.forced.srt", True), (".zh.sdh.srt", False)):
                with self.subTest(stem=stem, suffix=suffix):
                    track = self.plugin._external_track_from_path(self.directory / (stem + suffix), video)
                    self.assertEqual(track.forced, expected)
        self.assertFalse(self.plugin._external_track_from_path(Path("/forced/movie.en.srt"), None).forced)
        self.assertFalse(self.plugin._external_track_from_path(Path("Signs.2002.en.srt"), None).forced)
        self.assertTrue(self.plugin._external_track_from_path(Path("movie.en.forced.srt"), None).forced)

    def test_forced_embedded_disposition_and_metadata(self):
        for title, disposition, expected in (("The Forced Movie", 0, False), ("English forced", 0, True),
                                             ("English [forced]", 0, True), ("English", 1, True),
                                             ("Foreign.Correspondent.1940", 0, False)):
            stream = {"codec_type": "subtitle", "codec_name": "srt", "index": 0,
                      "tags": {"title": title}, "disposition": {"forced": disposition}}
            self.plugin._run_command = lambda *args, **kwargs: (True, json.dumps({"streams": [stream]}), "")
            self.assertEqual(self.plugin._probe_embedded_subtitles(Path("movie.mkv"))[0].forced, expected)

    def test_srt_tags_survive_translation_and_compression(self):
        text = '<i><font color="#FF0000">Hello</font></i> <b>World</b>'
        self.plugin._enable_line_check = True
        def completion(messages, config):
            payload = json.loads(messages[1]["content"])
            result = []
            for item in payload["items"]:
                tokens = self.formats.TAG_TOKEN.findall(item["text"])
                if "violations" in item:
                    value = tokens[0] + tokens[1] + "你好" + tokens[2] + tokens[3] + " " + tokens[4] + "世界" + tokens[5]
                else:
                    value = item["text"].replace("Hello", "这是一条超过十六个字的中文字幕需要进行压缩").replace("World", "世界")
                result.append({"index": item["index"], "text": value})
            return json.dumps(result, ensure_ascii=False)
        self.plugin._chat_completion = completion
        ok, message = self.translate(self.srt(text))
        self.assertTrue(ok, message)
        self.assertIn('<i><font color="#FF0000">你好</font></i> <b>世界</b>', self.output.read_text())

    def test_ass_overrides_and_non_text_fields_survive_compression(self):
        self.source = self.directory / "movie.en.ass"
        self.output = self.directory / "movie.zh.ass"
        self.plugin._enable_line_check = True
        content = '[Script Info]\nTitle: Example\n[Events]\nFormat: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.00,0:00:02.00,Default,{\\an8}{\\i1}Hello{\\i0}\n'
        def completion(messages, config):
            items = json.loads(messages[1]["content"])["items"]
            value = "短中文" if "violations" in items[0] else "这是一条超过十六个汉字的中文字幕需要压缩啊"
            return json.dumps([{"index": items[0]["index"], "text": value}], ensure_ascii=False)
        self.plugin._chat_completion = completion
        ok, message = self.translate(content)
        self.assertTrue(ok, message)
        self.assertIn('Default,{\\an8}{\\i1}短中文{\\i0}', self.output.read_text())
        self.assertIn('Title: Example', self.output.read_text())

    def test_lost_middle_tags_and_complex_tags_fail_without_writes(self):
        self.plugin._chat_completion = lambda *args: '[{"index":1,"text":"你好世界"}]'
        for text in ('Hello <i>World</i>', '{\\k20}Hello', '{\\p1}m 0 0 l 1 1', '<ruby>Hello</ruby>'):
            with self.subTest(text=text):
                self.output.write_bytes(b"old")
                ok, message = self.translate(self.srt(text))
                self.assertFalse(ok)
                self.assertEqual(self.output.read_bytes(), b"old")
                self.assertIn("标签", message)

    def test_token_order_corruption_rejected_in_all_translation_profiles(self):
        for profile in ("fast", "standard", "quality"):
            self.plugin._translation_profile = profile
            self.plugin._chat_completion = lambda *args: '[{"index":1,"text":"[[SH_TAG_001]]你好[[SH_TAG_000]]"}]'
            ok, message = self.translate(self.srt('<i>Hello</i>'))
            self.assertFalse(ok)
            self.assertIn("缺失 1 条", message)

    def test_lost_middle_tags_in_compression_fail(self):
        self.plugin._enable_line_check = True
        def completion(messages, config):
            item = json.loads(messages[1]["content"])["items"][0]
            text = "压缩译文" if "violations" in item else '这是一条明显超过十六个字的中文字幕译文 [[SH_TAG_000]]中文[[SH_TAG_001]]'
            return json.dumps([{"index": item["index"], "text": text}], ensure_ascii=False)
        self.plugin._chat_completion = completion
        self.output.write_bytes(b"old")
        ok, message = self.translate(self.srt('Hello <i>World</i>'))
        self.assertFalse(ok)
        self.assertIn("缺失 1 条", message)
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_replace_failure_and_bad_render_leave_source_and_output_unchanged(self):
        self.plugin._chat_completion = self.chinese_model
        self.output.write_bytes(b"old")
        with patch.object(self.ops.os, "replace", side_effect=OSError("disk error")):
            ok, message = self.translate(self.srt("Hello"))
        self.assertFalse(ok)
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertEqual(self.source.read_text(), self.srt("Hello"))
        self.assertFalse(list(self.directory.glob(".movie*")))
        self.plugin._render_srt = lambda cues: "bad rendering"
        ok, message = self.translate(self.srt("Hello"))
        self.assertFalse(ok)
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertFalse(list(self.directory.glob(".movie*")))

    def test_failed_embedded_extraction_preserves_existing_file(self):
        track = self.module.SubtitleTrack("embedded", None, self.directory / "movie.mkv", 0, "srt", "en", "English", False, False, True, ".srt")
        output = self.plugin._extracted_subtitle_path(track)
        output.write_bytes(b"old extraction")
        self.plugin._overwrite = True
        def run(command, **kwargs):
            Path(command[-1]).write_bytes(b"truncated")
            return False, "", "ffmpeg failed"
        self.plugin._run_command = run
        ok, path, message = self.plugin._extract_embedded_subtitle(track)
        self.assertFalse(ok)
        self.assertEqual(output.read_bytes(), b"old extraction")
        self.assertFalse(list(self.directory.glob(".movie*")))

    def test_401_403_400_fail_immediately_with_status_and_summary(self):
        self.plugin._api_retries = 10
        for status in (401, 403, 400, 404):
            self.responses = [self.response(status=status)]
            self.calls = []
            with self.fake_clock(), self.assertRaisesRegex(RuntimeError, f"HTTP {status}.*upstream rejected"):
                self.stage()
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.delays, [])

    def test_429_honors_retry_after(self):
        self.plugin._api_retries = 1
        self.responses = [self.response(status=429, headers={"Retry-After": "7"}),
                          self.response('[{"index":1,"text":"你好"}]')]
        with self.fake_clock():
            self.assertEqual(self.stage(), {1: "你好"})
        self.assertEqual(self.delays, [7.0])
        self.assertEqual(len(self.calls), 2)

    def test_retry_after_http_date_and_budget_exhaustion(self):
        self.plugin._api_retries = 10
        self.plugin._api_stage_budget = 5
        self.responses = [self.response(status=429, headers={"Retry-After": "Thu, 01 Jan 1970 00:00:10 GMT"})]
        with self.fake_clock(), patch.object(self.ops.time, "time", lambda: 0), self.assertRaisesRegex(RuntimeError, "总预算 5s"):
            self.stage()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.delays, [])

    def test_persistent_5xx_stops_inside_total_budget(self):
        self.plugin._api_retries = 100
        self.plugin._api_stage_budget = 9
        self.responses = [self.response(status=503) for _ in range(10)]
        with self.fake_clock(), self.assertRaisesRegex(RuntimeError, "总预算 9s.*HTTP 503"):
            self.stage()
        self.assertEqual(self.delays, [2.0, 4.0])
        self.assertEqual(len(self.calls), 3)
        self.assertLessEqual(self.clock, 9)
        self.assertEqual([call["timeout"] for call in self.calls], [9, 7, 3])

    def test_network_error_retries_and_late_success_rejected(self):
        self.plugin._api_retries = 1
        self.responses = [requests.ConnectionError("offline"), self.response('[{"index":1,"text":"你好"}]')]
        with self.fake_clock():
            self.assertEqual(self.stage(), {1: "你好"})
        self.assertEqual(self.delays, [2.0])
        self.plugin._api_stage_budget = 1
        def slow(*args, **kwargs):
            self.clock += 2
            return '[{"index":1,"text":"你好"}]'
        with patch.object(self.ops.time, "monotonic", lambda: self.clock), patch.object(self.plugin, "_chat_completion", slow):
            with self.assertRaisesRegex(RuntimeError, "总预算 1s"):
                self.stage()

    def test_glossary_and_compress_use_same_retry_classification(self):
        self.plugin._api_retries = 10
        cue = self.formats.parse_srt(self.srt("Hello"))[0]
        for invoke in (lambda: self.plugin._run_glossary_stage(1, 1, [cue], "movie", self.config),
                       lambda: self.stage("compress")):
            self.calls = []
            self.responses = [self.response(status=401)]
            with self.fake_clock(), self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                invoke()
            self.assertEqual(len(self.calls), 1)

    def test_cancel_during_retry_wait_propagates_without_further_request(self):
        self.plugin._api_retries = 10
        self.plugin._cancel_event = threading.Event()
        self.responses = [self.response(status=503)]
        def cancel(delay):
            self.plugin._cancel_event.set()
            self.plugin._raise_if_cancelled()
        with self.fake_clock(), patch.object(self.plugin, "_interruptible_sleep", cancel):
            with self.assertRaises(self.plugin._JobCancelled):
                self.stage()
        self.assertEqual(len(self.calls), 1)

    def test_cancel_before_publish_removes_temporary_and_preserves_output(self):
        self.plugin._chat_completion = self.chinese_model
        self.plugin._cancel_event = threading.Event()
        self.output.write_bytes(b"old")
        render = self.plugin._render_srt
        def cancel(cues):
            self.plugin._cancel_event.set()
            return render(cues)
        self.plugin._render_srt = cancel
        with self.assertRaises(self.plugin._JobCancelled):
            self.translate(self.srt("Hello"))
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertFalse(list(self.directory.glob(".movie*")))

    def test_compatible_config_mro_and_stage_budget_form(self):
        self.plugin.init_plugin({"api_retries": 0, "api_stage_budget": 12})
        self.assertEqual(self.plugin._api_retries, 0)
        self.assertEqual(self.plugin._current_config()["api_stage_budget"], 12)
        form, defaults = self.plugin.get_form()
        self.assertEqual(defaults["api_stage_budget"], 12)
        self.assertIn('api_stage_budget', json.dumps(form))
        self.plugin.init_plugin({})
        self.assertEqual(self.plugin._current_config()["api_stage_budget"], 120)
        names = [base.__name__ for base in self.module.SubtitleHunter.__mro__]
        self.assertLess(names.index("UIMixin"), names.index("_PluginBase"))
        self.assertLess(names.index("RuntimeOpsMixin"), names.index("_PluginBase"))
        self.assertEqual(self.plugin.plugin_config_prefix, "subtitle_hunter_")


class LegacyV2Tests(unittest.TestCase):
    """Run original functions with their actual assertions, preserving all source files."""
    def run_legacy(self, filename, name):
        original_path = list(sys.path)
        with ExitStack() as stack:
            stack.callback(lambda: sys.path.__setitem__(slice(None), original_path))
            stack.enter_context(patch.dict(sys.modules, {"pytest": pytest_substitute()}))
            conftest = load_file("_legacy_v2_conftest", ROOT / "tests/conftest.py")
            modules = conftest.plugin_modules()
            module = load_file("_legacy_" + filename[:-3], ROOT / "tests" / filename)
            temporary = stack.enter_context(tempfile.TemporaryDirectory())
            values = {"plugin_modules": modules, "tmp_path": Path(temporary),
                      "monkeypatch": types.SimpleNamespace(setattr=lambda obj, key, value:
                          stack.enter_context(patch.object(obj, key, value)))}
            if hasattr(module, "pure_modules"):
                values["pure_modules"] = module.pure_modules(modules)
            function = getattr(module, name)
            function(**{key: values[key] for key in inspect.signature(function).parameters})


# AST discovery neither imports pytest nor executes legacy tests during collection.
import ast
for _filename in ("test_contracts.py", "test_prowlarrextend.py", "test_subtitlehunter.py"):
    for _node in ast.parse((ROOT / "tests" / _filename).read_text()).body:
        if isinstance(_node, ast.FunctionDef) and _node.name.startswith("test_"):
            def _test(self, filename=_filename, name=_node.name):
                self.run_legacy(filename, name)
            setattr(LegacyV2Tests, "test_" + _filename[5:-3] + "__" + _node.name[5:], _test)


if __name__ == "__main__":
    unittest.main()
