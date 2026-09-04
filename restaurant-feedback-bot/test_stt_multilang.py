"""STT helpers: multilingual / Uzbek-friendly transcription defaults."""
from __future__ import annotations

import ai_advisor


def test_whisper_language_default_is_auto(monkeypatch):
    monkeypatch.delenv("WHISPER_LANGUAGE", raising=False)
    assert ai_advisor.whisper_language_hint(None) is None
    assert ai_advisor.whisper_language_hint("auto") is None
    assert ai_advisor.whisper_language_hint("none") is None


def test_whisper_language_env_and_explicit(monkeypatch):
    monkeypatch.setenv("WHISPER_LANGUAGE", "ru")
    assert ai_advisor.whisper_language_hint(None) == "ru"
    assert ai_advisor.whisper_language_hint("uz") == "uz"
    assert ai_advisor.whisper_language_hint("auto") is None


def test_whisper_prompt_mentions_uzbek(monkeypatch):
    monkeypatch.delenv("WHISPER_PROMPT", raising=False)
    p = ai_advisor.whisper_prompt_text().lower()
    assert "узбек" in p or "o'zbek" in p or "uzbek" in p


def test_looks_non_russian_latin_uzbek():
    assert ai_advisor.looks_non_russian(
        "Bugun smena juda yaxshi boldi, mijozlar mamnun"
    )
    assert ai_advisor.looks_non_russian(
        "Здоровый прекрасныйadı, unfortunately, он очень здоровый человек"
    )
    assert not ai_advisor.looks_non_russian(
        "Смена прошла нормально, команда держала ритм"
    )
