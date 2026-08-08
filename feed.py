"""Лента вакансий — одно место, где решается, что человек вообще видит.

Правила ленты (пересмотр продукта 08.08.2026):

- **Только открытые.** Закрытая (`closed`) или скрытая (`hidden`) вакансия в
  ленту не попадает никогда: подать на неё нельзя, а место в списке она
  занимает. Раньше список статусов-исключений был вписан руками в полутора
  десятках запросов — стоило забыть один, и мёртвые вакансии протекали.
- **Страна — настройка, а не константа.** По умолчанию Дания; список стран
  лежит в settings.json. Заложено сразу, чтобы выход за пределы DK стоил
  галочки, а не переписывания запросов.

Вакансию без указанной страны лента показывает всегда. Это сознательно:
страну не пишут вакансии, добавленные по ссылке вручную, и молчание источника
о стране не должно молча опустошать ленту. Отсекаем только то, про что точно
известно, что оно в чужой стране.
"""
from __future__ import annotations

from sqlmodel import func

import settings_store
from db import Job

# Статусы, которых в ленте не бывает ни при каких фильтрах.
CLOSED_STATUSES = ("closed", "hidden")

# «Любая страна» — явное значение настройки, а не пустой список: пустой список
# нельзя отличить от «настройку ещё не открывали».
ANY = "*"
DEFAULT_COUNTRIES = ("DK",)

# Как источники пишут страну. Ключ — код ISO-3166 alpha-2, который лежит в базе.
_SPELLINGS = {
    "DK": ("DK", "DNK", "DEN", "DENMARK", "DANMARK", "DÄNEMARK", "ДАНИЯ"),
    "SE": ("SE", "SWE", "SWEDEN", "SVERIGE", "SCHWEDEN", "ШВЕЦИЯ"),
    "NO": ("NO", "NOR", "NORWAY", "NORGE", "NORWEGEN", "НОРВЕГИЯ"),
    "DE": ("DE", "DEU", "GER", "GERMANY", "DEUTSCHLAND", "TYSKLAND", "ГЕРМАНИЯ"),
    "PL": ("PL", "POL", "POLAND", "POLEN", "POLSKA", "ПОЛЬША"),
    "NL": ("NL", "NLD", "NETHERLANDS", "HOLLAND", "NEDERLAND", "НИДЕРЛАНДЫ"),
    "UK": ("UK", "GB", "GBR", "UNITED KINGDOM", "GREAT BRITAIN", "ENGLAND",
           "ВЕЛИКОБРИТАНИЯ"),
    "IE": ("IE", "IRL", "IRELAND", "ИРЛАНДИЯ"),
    "FI": ("FI", "FIN", "FINLAND", "SUOMI", "ФИНЛЯНДИЯ"),
    "IS": ("IS", "ISL", "ICELAND", "ISLAND", "ИСЛАНДИЯ"),
    "ES": ("ES", "ESP", "SPAIN", "ESPAÑA", "SPANIEN", "ИСПАНИЯ"),
    "FR": ("FR", "FRA", "FRANCE", "FRANKRIG", "ФРАНЦИЯ"),
    "IT": ("IT", "ITA", "ITALY", "ITALIA", "ITALIEN", "ИТАЛИЯ"),
    "PT": ("PT", "PRT", "PORTUGAL", "ПОРТУГАЛИЯ"),
    "BE": ("BE", "BEL", "BELGIUM", "BELGIEN", "БЕЛЬГИЯ"),
    "AT": ("AT", "AUT", "AUSTRIA", "ÖSTERREICH", "АВСТРИЯ"),
    "CH": ("CH", "CHE", "SWITZERLAND", "SCHWEIZ", "ШВЕЙЦАРИЯ"),
    "CZ": ("CZ", "CZE", "CZECHIA", "CZECH REPUBLIC", "ЧЕХИЯ"),
    "LT": ("LT", "LTU", "LITHUANIA", "LIETUVA", "ЛИТВА"),
    "LV": ("LV", "LVA", "LATVIA", "LATVIJA", "ЛАТВИЯ"),
    "EE": ("EE", "EST", "ESTONIA", "EESTI", "ЭСТОНИЯ"),
    "UA": ("UA", "UKR", "UKRAINE", "УКРАИНА", "УКРАЇНА"),
}

# Русские подписи для интерфейса. Незнакомый код показываем как есть.
NAMES = {
    "DK": "Дания", "SE": "Швеция", "NO": "Норвегия", "DE": "Германия",
    "PL": "Польша", "NL": "Нидерланды", "UK": "Великобритания",
    "IE": "Ирландия", "FI": "Финляндия", "IS": "Исландия", "ES": "Испания",
    "FR": "Франция", "IT": "Италия", "PT": "Португалия", "BE": "Бельгия",
    "AT": "Австрия", "CH": "Швейцария", "CZ": "Чехия", "LT": "Литва",
    "LV": "Латвия", "EE": "Эстония", "UA": "Украина",
}

_BY_SPELLING = {
    spelling: code
    for code, spellings in _SPELLINGS.items()
    for spelling in spellings
}


def normalize(value) -> str:
    """«Danmark», «DNK», « dk » → «DK». Непонятное — пустая строка.

    Двухбуквенный код неизвестной нам страны принимаем как есть: список выше —
    подсказка для частых написаний, а не белый список стран мира.
    """
    if isinstance(value, dict):  # коннекторы иногда отдают {"name": …}
        value = value.get("name") or value.get("addressCountry") or value.get("value")
    text = " ".join(str(value or "").split()).strip(" .,;").upper()
    if not text:
        return ""
    if text in _BY_SPELLING:
        return _BY_SPELLING[text]
    if len(text) == 2 and text.isalpha():
        return text
    return ""


def name(code: str) -> str:
    """Русская подпись страны для интерфейса."""
    if str(code or "").strip() == ANY:
        return "любая страна"
    code = normalize(code) or str(code or "").upper()
    return NAMES.get(code, code)


# Настройки ленты спрашивают в цикле по вакансиям (приём каталога, проверки
# ленты), а settings_store.load() читает файл с диска. Держим разобранное
# значение до следующей записи settings.json — сверяемся по времени изменения.
_cache: dict = {"stamp": None, "data": None}


def _settings() -> dict:
    path = settings_store.PATH
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (str(path), 0, 0)
    if _cache["data"] is not None and _cache["stamp"] == stamp:
        return _cache["data"]
    data = settings_store.load()
    _cache.update(stamp=stamp, data=data)
    return data


def _forget() -> None:
    """Сбросить кэш настроек после записи, не дожидаясь mtime."""
    _cache.update(stamp=None, data=None)


def countries() -> list[str]:
    """Страны, вакансии которых показываем. `["*"]` — любые."""
    return _parse(_settings().get("countries"))


def _parse(raw) -> list[str]:
    if raw is None:
        return list(DEFAULT_COUNTRIES)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_COUNTRIES)
    if ANY in raw:
        return [ANY]
    codes = []
    for item in raw:
        code = normalize(item)
        if code and code not in codes:
            codes.append(code)
    # Пустой результат означал бы ленту без единой вакансии — это не настройка,
    # а поломка. Возвращаемся к значению по умолчанию.
    return codes or list(DEFAULT_COUNTRIES)


def set_countries(values) -> list[str]:
    """Сохранить страны ленты. Пустой выбор — Дания, `["*"]` — любая страна."""
    if isinstance(values, str):
        values = [values]
    codes = _parse(list(values or []))
    settings_store.mutate(lambda data: data.__setitem__("countries", codes))
    _forget()
    return codes


def any_country() -> bool:
    """Выбран ли режим «любая страна»."""
    return countries() == [ANY]


def allows(value, unknown_ok: bool = True) -> bool:
    """Показываем ли вакансию из этой страны.

    unknown_ok=False — для приёма новых вакансий из каталогов ATS: там страну
    без опознания лучше не тащить в базу, иначе в ленту польётся весь мир.
    Режим «любая страна» отменяет и это: человек явным выбором сказал «всё».
    """
    if any_country():
        return True
    code = normalize(value)
    if not code:
        return bool(unknown_ok)
    return code in countries()


def country_clause():
    """Условие SQL «страна разрешена настройкой» (или None — фильтровать нечего).

    Сравниваем по всем известным написаниям: в базе лежит то, что прислал
    источник, а он может писать «Denmark» вместо «DK».
    """
    if any_country():
        return None
    spellings = set()
    for code in countries():
        spellings.update(_SPELLINGS.get(code, (code,)))
        spellings.add(code)
    return (
        Job.country.is_(None)
        | (func.trim(Job.country) == "")
        | func.upper(func.trim(Job.country)).in_(sorted(spellings))
    )


# ── Языковой барьер (шаг 2): вердикт считает relevance.py ──────────────────
def hide_barrier() -> bool:
    """Убирать ли из ленты вакансии, где точно нужен датский или местный диплом.

    По умолчанию ДА: аудитория WexFlow — люди без датского, и вакансия, куда их
    не возьмут, — такой же мусор, как закрытая. Скрывается только УВЕРЕННОЕ
    «не подойдёт»: «не ясно» остаётся видимым, а число скрытых лента показывает
    строкой «скрыто N — показать».
    """
    value = _settings().get("hide_barrier")
    return True if value is None else bool(value)


def set_hide_barrier(enabled: bool) -> bool:
    enabled = bool(enabled)
    settings_store.mutate(lambda data: data.__setitem__("hide_barrier", enabled))
    _forget()
    return enabled


def barrier_clause():
    """Условие SQL «языковой барьер не подтверждён» (или None — не фильтруем).

    NULL (ещё не оценивали) и «не ясно» проходят: молчание оценщика не повод
    прятать вакансию.
    """
    if not hide_barrier():
        return None
    import relevance
    return Job.fit.is_(None) | Job.fit.not_in(list(relevance.BARRIER))


def visible_clauses(exclude_applied: bool = False, fit: bool = True) -> list:
    """Условия ленты для `select(Job).where(*feed.visible_clauses())`.

    exclude_applied=True — ещё и без уже поданных (списки «активных»).
    fit=False — не применять языковой фильтр: нужно самому оценщику
    (relevance.py), иначе скрытая вакансия никогда бы не переоценивалась.
    """
    statuses = list(CLOSED_STATUSES) + (["applied"] if exclude_applied else [])
    clauses = [Job.status.not_in(statuses)]
    country = country_clause()
    if country is not None:
        clauses.append(country)
    if fit:
        barrier = barrier_clause()
        if barrier is not None:
            clauses.append(barrier)
    return clauses


def visible(job, exclude_applied: bool = False, fit: bool = True) -> bool:
    """То же правило для уже загруженной вакансии (без похода в базу)."""
    status = str(getattr(job, "status", "") or "")
    if status in CLOSED_STATUSES or (exclude_applied and status == "applied"):
        return False
    if not allows(getattr(job, "country", None)):
        return False
    if fit and hide_barrier():
        import relevance
        return str(getattr(job, "fit", "") or "") not in relevance.BARRIER
    return True
