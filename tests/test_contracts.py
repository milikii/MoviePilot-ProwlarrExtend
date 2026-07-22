import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def pure_modules(plugin_modules):
    """Ensure MoviePilot stubs are installed before importing plugin packages."""
    from prowlarrextend import mapping as prowlarr_mapping
    from subtitlehunter import codes, eta, formats, models, quality

    return {
        "prowlarr_mapping": prowlarr_mapping,
        "codes": codes,
        "eta": eta,
        "formats": formats,
        "models": models,
        "quality": quality,
    }


def test_prowlarr_search_fixture_mapping(pure_modules):
    mapping = pure_modules["prowlarr_mapping"]
    data = json.loads((FIXTURES / "prowlarr_search_sample.json").read_text(encoding="utf-8"))
    movie, show = data

    assert mapping.normalize_imdb_id(movie["imdbId"]) == "tt1234567"
    assert mapping.normalize_imdb_id(show["imdbId"]) == "tt7654321"
    assert "T" not in mapping.normalize_pubdate(movie["publishDate"])
    assert mapping.normalize_pubdate(movie["publishDate"]).count(":") == 2

    dl, ul = mapping.volume_factors(movie)
    assert dl == 0.0
    assert ul == 2.0

    dl2, ul2 = mapping.volume_factors({
        "downloadVolumeFactor": 0.5,
        "uploadVolumeFactor": 1.0,
        "indexerFlags": ["freeleech", "doubleupload"],
    })
    assert dl2 == 0.5
    assert ul2 == 1.0

    half_dl, half_ul = mapping.volume_factors(show)
    assert half_dl == 0.5
    assert half_ul == 1.0

    assert mapping.infer_category(movie["categories"]) == "电影"
    assert mapping.infer_category(show["categories"]) == "电视剧"


def test_translation_json_contract_and_quality_gate(pure_modules):
    quality = pure_modules["quality"]
    formats = pure_modules["formats"]
    SubtitleCue = pure_modules["models"].SubtitleCue

    fenced = """```json
[
  {"index": 1, "text": "你好世界"},
  {"index": 2, "text": "这是中文译文"}
]
```"""
    parsed = quality.parse_translation_response(fenced)
    assert parsed == {1: "你好世界", 2: "这是中文译文"}
    assert formats.extract_json_array(fenced).startswith("[")

    source = [
        SubtitleCue(1, "00:00:01,000", "00:00:02,000", "Hello world"),
        SubtitleCue(2, "00:00:02,000", "00:00:03,000", "This is English"),
    ]
    good = [
        SubtitleCue(1, "00:00:01,000", "00:00:02,000", "你好世界"),
        SubtitleCue(2, "00:00:02,000", "00:00:03,000", "这是中文译文"),
    ]
    ok, message = quality.validate_translated_cues(source, good)
    assert ok is True
    assert message == ""

    bad_english = [
        SubtitleCue(1, "00:00:01,000", "00:00:02,000", "Hello world"),
        SubtitleCue(2, "00:00:02,000", "00:00:03,000", "Still English"),
    ]
    ok, message = quality.validate_translated_cues(source, bad_english)
    assert ok is False
    assert "中文内容覆盖不足" in message


def test_srt_roundtrip_contract(pure_modules):
    formats = pure_modules["formats"]
    content = "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:02,000 --> 00:00:03,000\nWorld\n"
    cues = formats.parse_srt(content)
    assert [cue.text for cue in cues] == ["Hello", "World"]
    rendered = formats.render_srt(cues)
    again = formats.parse_srt(rendered)
    assert [cue.text for cue in again] == ["Hello", "World"]


def test_eta_and_failure_code_contracts(pure_modules):
    eta = pure_modules["eta"]
    codes = pure_modules["codes"]
    assert eta.estimate_eta_seconds(10, 9000, "fast", 9000, 1) == eta.estimate_eta_seconds(1, 9000, "fast", 9000, 1)
    assert codes.map_error_message("任务已取消") == "cancelled"
    assert codes.map_error_message("中文内容覆盖不足，模型可能未完成翻译") == "translate_quality"
