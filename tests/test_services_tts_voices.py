def test_voice_catalogue_lists_all_documented_voices():
    from app.services.tts_voices import VOICES

    expected = {
        ("de",    "thorsten",                frozenset({"low", "medium", "high"})),
        ("de",    "thorsten_emotional",      frozenset({"medium"})),
        ("de",    "kerstin",                 frozenset({"low"})),
        ("en_US", "lessac",                  frozenset({"low", "medium", "high"})),
        ("en_US", "amy",                     frozenset({"low", "medium"})),
        ("en_US", "ryan",                    frozenset({"low", "medium", "high"})),
        ("en_GB", "alba",                    frozenset({"medium"})),
        ("en_GB", "southern_english_female", frozenset({"low", "medium"})),
        ("fr",    "siwis",                   frozenset({"low", "medium"})),
        ("es",    "sharvard",                frozenset({"medium"})),
    }
    seen = {(v.language, v.id, frozenset(v.qualities)) for v in VOICES}
    assert seen == expected


def test_voices_for_language_filters_correctly():
    from app.services.tts_voices import voices_for_language
    voices = voices_for_language("de")
    ids = {v.id for v in voices}
    assert ids == {"thorsten", "thorsten_emotional", "kerstin"}


def test_qualities_for_voice_filters_correctly():
    from app.services.tts_voices import qualities_for_voice
    assert qualities_for_voice("de", "thorsten") == ("low", "medium", "high")
    assert qualities_for_voice("de", "thorsten_emotional") == ("medium",)


def test_voice_file_path_uses_huggingface_naming():
    from pathlib import Path
    from app.services.tts_voices import voice_file_path

    base = Path("/data/tts-voices")
    p = voice_file_path(base, "de", "thorsten", "medium")
    assert p == base / "de_DE-thorsten-medium.onnx"


def test_voice_download_url_constructs_correctly():
    from app.services.tts_voices import voice_download_urls
    onnx_url, json_url = voice_download_urls("de", "thorsten", "medium")
    assert onnx_url == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
        "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"
    )
    assert json_url == onnx_url + ".json"


def test_voice_download_url_en_us_uses_correct_region():
    from app.services.tts_voices import voice_download_urls
    onnx_url, _ = voice_download_urls("en_US", "lessac", "high")
    assert "en/en_US/lessac/high/en_US-lessac-high.onnx" in onnx_url


def test_voice_download_url_en_gb_uses_correct_region():
    from app.services.tts_voices import voice_download_urls
    onnx_url, _ = voice_download_urls("en_GB", "alba", "medium")
    assert "en/en_GB/alba/medium/en_GB-alba-medium.onnx" in onnx_url
