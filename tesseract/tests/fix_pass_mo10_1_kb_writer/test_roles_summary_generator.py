"""MO-10-1 §2d — pure-Python SUMMARY.md renders the role wiring table."""

from __future__ import annotations

from tesseract.scripts.regenerate_roles_summary import regenerate, render_summary


def test_render_summary_includes_role_rows():
    roles_doc = {
        "embeddings": {"primary": "local.ollama.nomic_embed"},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "primary": "api.openai.gpt54_mini",
                "fallbacks": ["api.openai.gpt54_nano", "api.google.gemini_25_flash"],
            },
            "observer_agent": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
                "fallbacks": [],
            },
        },
        "voice": {
            "stt": {"mode": "active", "primary": "local.whisper.local_whisper", "fallbacks": []},
            "tts": {"mode": "active", "primary": "local.piper.northern_english_male", "fallbacks": []},
        },
    }
    providers_doc = {
        "api": {
            "openai": {"adapter": "openai"},
            "google": {"adapter": "gemini"},
        },
        "local": {
            "ollama": {"adapter": "ollama"},
            "whisper": {"adapter": "local_whisper"},
            "piper": {"adapter": "piper"},
        },
    }
    text = render_summary(roles_doc, providers_doc)
    assert "TARS roles — current wiring" in text
    assert "| chat_brain |" in text
    assert "`api.openai.gpt54_mini`" in text
    assert "| openai |" in text  # adapter cell
    assert "Voice lanes" in text
    assert "Embeddings" in text


def test_regenerate_writes_to_default_target(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    # Use the live config files — they're the canonical wiring and the
    # test ensures the rendered text reflects them without crashes.
    out = regenerate()
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "TARS roles — current wiring" in text
    assert "generated_at:" in text
