"""Перевод описаний вакансий с локальным кэшем в SQLite.

Основной бесплатный вариант без API — Argos Translate (офлайн).
DeepL остаётся опциональным, если пользователь сам добавит ключ.
"""
import html
import re
from urllib.parse import quote

import httpx

import config


class TranslationError(Exception):
    pass


def _argos_pair_available() -> bool:
    try:
        import argostranslate.translate
        return bool(argostranslate.translate.get_translation_from_codes("da", "ru"))
    except Exception:
        return False


def configured() -> bool:
    return True


def provider_name() -> str:
    if config.DEEPL_API_KEY:
        return "DeepL"
    if _argos_pair_available():
        return "Argos Translate offline"
    return "Google Translate"


def _plain_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _plain_text_to_html(text: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n{2,}", text or "") if b.strip()]
    if not blocks:
        return ""
    return "\n".join(f"<p>{html.escape(block).replace(chr(10), '<br>')}</p>" for block in blocks)


def _translate_offline_to_ru(source_html: str) -> str:
    try:
        import argostranslate.translate
    except Exception as exc:
        raise TranslationError("Argos Translate is not installed") from exc

    translator = argostranslate.translate.get_translation_from_codes("da", "ru")
    if not translator:
        raise TranslationError("Argos model da→ru is not installed")

    text = _plain_text(source_html)
    if not text:
        return ""

    translated_blocks = []
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        translated_blocks.append(translator.translate(block))
    return _plain_text_to_html("\n\n".join(translated_blocks))


def _translate_google_to_ru(source_html: str) -> str:
    """No-key fallback through Google's public translate endpoint.

    It is not as clean as a paid API, but it gives the one-button behavior:
    click -> translate text -> show Russian.
    """
    text = _plain_text(source_html)
    if not text:
        return ""

    translated_blocks = []
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        chunks = [block[i:i + 3800] for i in range(0, len(block), 3800)]
        out = []
        for chunk in chunks:
            response = httpx.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": "auto",
                    "tl": "ru",
                    "dt": "t",
                    "q": chunk,
                },
                timeout=30,
            )
            if response.status_code != 200:
                raise TranslationError(f"Translate returned {response.status_code}")
            data = response.json()
            out.append("".join(part[0] for part in data[0] if part and part[0]))
        translated_blocks.append("".join(out))
    return _plain_text_to_html("\n\n".join(translated_blocks))


def translate_to_ru(source_html: str, *, title: str = "") -> str:
    if config.DEEPL_API_KEY:
        return translate_html_to_ru(source_html, title=title)
    if _argos_pair_available():
        return _translate_offline_to_ru(source_html)
    return _translate_google_to_ru(source_html)


def translate_html_to_ru(html: str, *, title: str = "") -> str:
    """Переводит HTML-описание вакансии на русский.

    DeepL умеет HTML tag handling, поэтому структура описания сохраняется лучше,
    чем при переводе уже очищенного plain text.
    """
    if not configured():
        raise TranslationError("DeepL API key is not configured")
    text = (html or "").strip()
    if not text:
        return ""
    if len(text.encode("utf-8")) > 120 * 1024:
        text = _plain_text(text)[:45000]

    payload = {
        "text": [text],
        "target_lang": "RU",
        "tag_handling": "html",
        "preserve_formatting": True,
    }
    if title:
        payload["context"] = f"Job vacancy description. Job title: {title}"

    try:
        response = httpx.post(
            config.DEEPL_API_URL,
            headers={
                "Authorization": f"DeepL-Auth-Key {config.DEEPL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise TranslationError(str(exc)) from exc

    if response.status_code != 200:
        detail = response.text[:300]
        raise TranslationError(f"DeepL returned {response.status_code}: {detail}")

    data = response.json()
    try:
        return data["translations"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslationError("DeepL response did not contain translated text") from exc
