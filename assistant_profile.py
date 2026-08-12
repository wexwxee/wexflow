"""Правка профиля голосом: что помощнику можно менять, а что — только человеку.

Иван попросил помощника «как Джарвис»: чтобы он менял настройки и профиль по
просьбе, а не только искал. Разница между полезным и опасным здесь проходит не
по сложности, а по последствиям.

Всё, что лежит в профиле, рано или поздно уезжает в НАСТОЯЩУЮ анкету под именем
человека, и отозвать её нельзя. Поэтому поля разделены на две группы:

- **Мягкие** — предпочтения и обстоятельства: город, языки, опыт, готовность к
  сменам, дата выхода. Ошибка здесь стоит одной правки: помощник меняет сразу,
  честно показывает «было → стало» и умеет вернуть обратно.
- **Твёрдые** — личность, связь и юридические ответы: имя, фамилия, почта,
  телефон, адрес, гражданство, разрешение на работу, справка о несудимости, CV.
  Их помощник не трогает вообще, даже когда его прямо просят: расслышанный
  наполовину телефон в отправленной анкете — это провал, который человек
  заметит слишком поздно. Вместо правки — ссылка на профиль, где видно всё поле
  целиком.

Это не осторожность ради осторожности: цена ошибки в двух группах отличается на
порядок, и граница проведена именно по ней.
"""
from __future__ import annotations

import datetime as dt
import re

import profile_store

# Мягкие поля: ключ → (как называем человеку, тип значения).
SOFT_FIELDS: dict[str, tuple[str, str]] = {
    "city": ("город", "text"),
    "zip": ("индекс", "zip"),
    "languages": ("языки", "text"),
    "experience_years": ("лет опыта", "text"),
    "current_role": ("текущая должность", "text"),
    "available_from": ("готов выйти с", "text"),
    "about": ("о себе", "long"),
    "two_year_goal": ("цель на два года", "long"),
    "start_date": ("дата выхода", "date"),
    "retail_experience": ("опыт в рознице", "yesno"),
    "warehouse_experience": ("опыт на складе", "yesno"),
    "english_work": ("английский для работы", "yesno"),
    "work_weekends": ("готов работать по выходным", "yesno"),
    "work_evenings": ("готов на вечерние смены", "yesno"),
    "work_early": ("готов выходить рано утром", "yesno"),
    "work_night": ("готов на ночные смены", "yesno"),
    "has_drivers_license": ("водительские права", "yesno"),
}

# Твёрдые поля: помощник их только показывает и отправляет в профиль.
HARD_FIELDS: dict[str, str] = {
    "first_name": "имя",
    "last_name": "фамилия",
    "email": "почта",
    "phone": "телефон",
    "address": "адрес",
    "date_of_birth": "дата рождения",
    "citizenship": "гражданство",
    "work_permit": "разрешение на работу",
    "criminal_record": "справка о несудимости",
    "cv_path": "резюме",
    "cover_letter_path": "сопроводительное письмо",
}

# Как человек называет поля вслух. Ключ — то, что ищем в тексте.
ALIASES: dict[str, str] = {
    "город": "city", "живу в": "city", "индекс": "zip", "почтовый индекс": "zip",
    "язык": "languages", "языки": "languages",
    "опыт": "experience_years", "стаж": "experience_years",
    "должность": "current_role", "кем работаю": "current_role",
    "о себе": "about", "цель": "two_year_goal",
    "дата выхода": "start_date", "выйти с": "start_date", "могу выйти": "start_date",
    "розниц": "retail_experience", "магазин": "retail_experience",
    "склад": "warehouse_experience",
    "английск": "english_work",
    "выходн": "work_weekends", "вечерн": "work_evenings",
    "утренн": "work_early", "рано": "work_early", "ночн": "work_night",
    "права": "has_drivers_license", "водительск": "has_drivers_license",
    "имя": "first_name", "фамилия": "last_name", "почта": "email",
    "email": "email", "телефон": "phone", "номер": "phone", "адрес": "address",
    "дата рождения": "date_of_birth", "гражданств": "citizenship",
    "разрешение": "work_permit", "судимост": "criminal_record",
    "резюме": "cv_path", "cv": "cv_path",
}

_YES = ("да", "есть", "готов", "могу", "умею", "yes", "true", "1")
_NO = ("нет", "не готов", "не могу", "не умею", "no", "false", "0")


def human_name(key: str) -> str:
    if key in SOFT_FIELDS:
        return SOFT_FIELDS[key][0]
    return HARD_FIELDS.get(key, key)


def _clean_yesno(value: str) -> str:
    low = str(value or "").strip().lower()
    if any(low.startswith(word) for word in _NO):
        return "no"
    if any(low.startswith(word) for word in _YES):
        return "yes"
    return ""


def _clean_date(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            dt.date.fromisoformat(text)
            return text
        except ValueError:
            return ""
    match = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


def clean_value(key: str, value) -> tuple[str, str]:
    """Привести значение к виду профиля. Возвращает (значение, что не так)."""
    kind = SOFT_FIELDS.get(key, ("", "text"))[1]
    raw = " ".join(str(value or "").split())
    if not raw:
        return "", "пустое значение"
    if kind == "yesno":
        answer = _clean_yesno(raw)
        return (answer, "") if answer else ("", "нужно «да» или «нет»")
    if kind == "date":
        answer = _clean_date(raw)
        return (answer, "") if answer else ("", "нужна дата вида 2026-09-01")
    if kind == "zip":
        digits = re.sub(r"\D", "", raw)
        return (digits, "") if 3 <= len(digits) <= 6 else ("", "индекс — это 4 цифры")
    limit = 600 if kind == "long" else 120
    return raw[:limit], ""


def display(key: str, value: str) -> str:
    """Как показать значение человеку: «да»/«нет» вместо yes/no, пусто — «пусто»."""
    text = str(value or "").strip()
    if not text:
        return "пусто"
    if SOFT_FIELDS.get(key, ("", ""))[1] == "yesno":
        return {"yes": "да", "no": "нет"}.get(text, text)
    return text


def guess_field(text: str) -> str:
    """Поле, о котором говорит человек. Пусто — не поняли, и это нормально."""
    low = " ".join(str(text or "").lower().split())
    best = ""
    best_at = len(low) + 1
    for alias, key in ALIASES.items():
        at = low.find(alias)
        if at >= 0 and (at < best_at or (at == best_at and len(alias) > len(best))):
            best, best_at = key, at
    return best


def apply_change(key: str, value) -> dict:
    """Записать мягкое поле. Возвращает описание того, что произошло."""
    if key in HARD_FIELDS:
        return {"ok": False, "reason": "hard", "field": key}
    if key not in SOFT_FIELDS:
        return {"ok": False, "reason": "unknown", "field": key}
    clean, problem = clean_value(key, value)
    if problem:
        return {"ok": False, "reason": "bad_value", "field": key, "problem": problem}

    before = {"value": ""}

    def updater(profile: dict):
        before["value"] = str(profile.get(key) or "")
        profile[key] = clean
        return profile

    profile_store.mutate_profile(updater)
    return {
        "ok": True, "field": key, "human": human_name(key),
        "before": before["value"], "after": clean,
    }
