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


_AI_PROMPT = (
    "Переведи описание вакансии на русский язык.\n"
    "Правила:\n"
    "1. Переводи ТОЛЬКО то, что написано. Ничего не добавляй, не убирай и не "
    "пересказывай своими словами — человек принимает по этому тексту решение о работе.\n"
    "2. Сохрани структуру: абзацы, списки (каждый пункт с новой строки, начиная с «- »), "
    "заголовки разделов.\n"
    "3. Названия компаний, магазинов, городов и адреса оставь как есть.\n"
    "4. Числа, часы в неделю, даты и суммы перенеси без изменений.\n"
    "5. Верни только перевод, без пояснений и без markdown-разметки вроде ```.\n"
)


def ai_available() -> bool:
    """Подключён ли ИИ, которым можно перевести описание."""
    try:
        import ai_gateway
        return ai_gateway.available()
    except Exception:  # noqa: BLE001 — отсутствие ИИ не должно ломать перевод
        return False


def _translate_ai_to_ru(source_html: str, *, title: str = "") -> tuple[str, str]:
    """Перевод подключённым ИИ. Возвращает (html, имя движка)."""
    import ai_gateway

    text = _plain_text(source_html)
    if not text:
        return "", ""
    # Описания вакансий короткие, но подрезаем на всякий случай: длинный ответ
    # упрётся в лимит токенов и вернётся обрубленным.
    if len(text) > 12000:
        text = text[:12000]
    prompt = _AI_PROMPT
    if title:
        prompt += f"\nНазвание вакансии: {title}\n"
    prompt += "\nТекст вакансии:\n" + text
    result = ai_gateway.generate_text(
        prompt, temperature=0.1, max_tokens=4096, timeout=90.0,
    )
    if not result.ok:
        raise TranslationError(result.error_message or "ИИ не ответил")
    reply = (result.reply or "").strip()
    # Модель иногда оборачивает ответ в ```; текст от этого не страдает, но
    # в готовом переводе такие «рёбра» выглядят как мусор.
    reply = re.sub(r"^```[a-z]*\s*|\s*```$", "", reply).strip()
    if not reply:
        raise TranslationError("ИИ вернул пустой перевод")
    engine = f"ИИ ({result.provider}{', ' + result.model if result.model else ''})"
    return _plain_text_to_html(reply), engine


def translate_to_ru_with_engine(
    source_html: str, *, title: str = "", prefer_ai: bool = False,
) -> tuple[str, str]:
    """Перевести описание и сказать, чем именно. Возвращает (html, движок).

    ИИ подключается только по явной кнопке (prefer_ai): фоновый перевод всех
    вакансий подряд сжёг бы бесплатную квоту за один проход. Если ИИ не
    ответил, молча уходим на обычный переводчик — человек всё равно получит
    русский текст.
    """
    if prefer_ai and ai_available():
        try:
            html_ru, engine = _translate_ai_to_ru(source_html, title=title)
            if html_ru:
                return html_ru, engine
        except Exception as exc:  # noqa: BLE001 — падаем на обычный переводчик
            print(f"перевод ИИ не удался, беру обычный переводчик: {str(exc)[:140]}")
    if config.DEEPL_API_KEY:
        return translate_html_to_ru(source_html, title=title), "DeepL"
    if _argos_pair_available():
        return _translate_offline_to_ru(source_html), "Argos Translate offline"
    return _translate_google_to_ru(source_html), "Google Translate"


def translate_to_ru(source_html: str, *, title: str = "") -> str:
    return translate_to_ru_with_engine(source_html, title=title)[0]


def translate_html_to_ru(html: str, *, title: str = "") -> str:
    """Переводит HTML-описание вакансии на русский.

    DeepL умеет HTML tag handling, поэтому структура описания сохраняется лучше,
    чем при переводе уже очищенного plain text.
    """
    if not config.DEEPL_API_KEY:
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
