"""
Unified translation service.

Priority order:
  1. DeepL API — best quality for European languages, very cheap
  2. Claude (Anthropic) — fallback for languages DeepL doesn't support,
     and for context-aware meeting jargon when DeepL isn't configured

Required env vars (at least one):
    DEEPL_API_KEY        – DeepL API key (Free tier: 500k chars/month free)
    ANTHROPIC_API_KEY    – Already required for minutes formatting

Optional:
    TRANSLATION_BACKEND  – "deepl" | "claude" | "auto" (default: auto)
                           auto = try DeepL first, fall back to Claude

Usage:
    from services.media.translation import translate_text
    result = translate_text("Bonjour", "FR", "EN")
"""

import os
import logging

log = logging.getLogger(__name__)

# Languages supported by DeepL (source codes).
# DeepL uses uppercase codes; we normalise from Whisper's lowercase.
_DEEPL_SUPPORTED_SOURCES = {
    "ar", "bg", "cs", "da", "de", "el", "en", "es", "et", "fi",
    "fr", "hu", "id", "it", "ja", "ko", "lt", "lv", "nb", "nl",
    "pl", "pt", "ro", "ru", "sk", "sl", "sv", "tr", "uk", "zh",
}

# DeepL target codes need regional variants for some languages
_DEEPL_TARGET_MAP = {
    "en": "EN-GB",
    "pt": "PT-PT",
    "zh": "ZH-HANS",
}


def _deepl_target_code(lang: str) -> str:
    """Convert a 2-letter ISO code to the DeepL target code."""
    return _DEEPL_TARGET_MAP.get(lang.lower(), lang.upper())


def _deepl_source_code(lang: str) -> str | None:
    """Return the DeepL source code if supported, else None."""
    code = lang.lower()
    # Norwegian: Whisper returns "no", DeepL uses "NB"
    if code == "no":
        return "NB"
    if code in _DEEPL_SUPPORTED_SOURCES:
        return code.upper()
    return None


def _translate_deepl(text: str, source_lang: str, target_lang: str) -> str:
    """
    Translate text using the DeepL API.
    Raises RuntimeError if the key is missing or language unsupported.
    """
    api_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPL_API_KEY not set")

    src = _deepl_source_code(source_lang)
    if not src:
        raise RuntimeError(f"DeepL does not support source language: {source_lang}")

    tgt = _deepl_target_code(target_lang)

    import deepl
    translator = deepl.Translator(api_key)
    result = translator.translate_text(
        text,
        source_lang=src,
        target_lang=tgt,
    )
    return result.text.strip()


def _translate_claude(
    text: str,
    source_lang_name: str,
    target_lang_name: str,
) -> str:
    """Translate text using Claude Haiku — cheaper model, translation-only task."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Translate the following text from {source_lang_name} to {target_lang_name}. "
                f"Return only the translation, nothing else.\n\n{text}"
            ),
        }],
    )
    return msg.content[0].text.strip()


def translate_text(
    text: str,
    source_lang: str,       # ISO 639-1 code, e.g. "fr"
    target_lang: str,       # ISO 639-1 code, e.g. "en"
    source_lang_name: str = "",   # human name, e.g. "French" (for Claude fallback)
    target_lang_name: str = "",   # human name, e.g. "English" (for Claude fallback)
) -> tuple[str, str]:
    """
    Translate text from source_lang to target_lang.

    Returns (translated_text, backend_used) where backend_used is "deepl" or "claude".

    Routing:
      - If TRANSLATION_BACKEND=deepl   → DeepL only (error if unsupported)
      - If TRANSLATION_BACKEND=claude  → Claude only
      - auto (default)                 → DeepL if available + supported, else Claude
    """
    if not text.strip():
        return "", "none"

    backend_setting = os.environ.get("TRANSLATION_BACKEND", "auto").lower()

    if backend_setting == "deepl":
        return _translate_deepl(text, source_lang, target_lang), "deepl"

    if backend_setting == "claude":
        return _translate_claude(text, source_lang_name or source_lang, target_lang_name or target_lang), "claude"

    # auto mode: try DeepL first, fall back to Claude
    deepl_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if deepl_key and _deepl_source_code(source_lang):
        try:
            result = _translate_deepl(text, source_lang, target_lang)
            log.debug("Translation via DeepL: %s→%s", source_lang, target_lang)
            return result, "deepl"
        except Exception as exc:
            log.warning("DeepL failed (%s), falling back to Claude.", exc)

    # Claude fallback
    result = _translate_claude(
        text,
        source_lang_name or source_lang.upper(),
        target_lang_name or target_lang.upper(),
    )
    log.debug("Translation via Claude: %s→%s", source_lang, target_lang)
    return result, "claude"
