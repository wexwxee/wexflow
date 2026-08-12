"""Разбор поискового запроса: «нетто херлев 15 часов до 18» → поля фильтра.

Зачем. Человек ищет работу не ключевыми словами, а фразой: «нетто херлев»,
«кассир 15 часов», «склад до 18». Раньше вся фраза уходила одной строкой в
LIKE по названию и описанию — и «Netto Herlev» не находило ничего, потому что
такой строки внутри вакансии нет. Магазин, город, часы и возраст — это разные
поля, и разбирать их надо до запроса, а не надеяться на подстроку.

Почему без ИИ. Словарь тут закрытый и крошечный: ~40 магазинов
(`labels.BRAND_ALIASES`), ~200 городов и районов (`labels.CITY_ALIASES`,
`CITY_GROUPS`), ~60 профессий (`ru_search.RU_DA`). На таком словаре обычный
разбор точнее модели, работает мгновенно, не тратит квоту и — главное —
не ломается, когда ключа ИИ нет. Поиск это ядро приложения, он не имеет права
зависеть от чужого сервиса.

Три обещания модуля:
  1. **Ничего не выдумывать.** Слово становится городом или магазином, только
     если оно есть в словаре (или в списке городов самой базы). Непонятое
     остаётся обычным текстовым поиском, как раньше.
  2. **Говорить, что понял.** Каждое решение попадает в `notes`, интерфейс
     показывает их строкой «Понял так: …». Исправленную опечатку человек
     видит и может отменить (`exact=True`).
  3. **Не сужать молча.** Часы и возраст нельзя проверить в SQL (часы лежат
     строкой «15-20», «37,5 t/uge»), поэтому их считает `python_filter`, и он
     возвращает, сколько и по какой причине отсеял.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import labels
import ru_search
from db import Job

# Опечатку исправляем только у слов подлиннее и только при высоком сходстве:
# «нето» → «нетто» это помощь, а «касса» → «кассир» уже додумывание за человека.
FUZZY_MIN_LEN = 4
FUZZY_CUTOFF = 0.8

# Голое «15 часов» — это «примерно 15», а не точное равенство. Разбег
# показываем человеку словами, чтобы он мог уточнить «до 15» или «от 15».
BARE_HOURS_SPREAD = 5.0

_HOURS_WORD = r"(?:часов|часа|час|ч|t/uge|timer|time)"
_RE_HOURS_RANGE = re.compile(rf"\bот\s*(\d{{1,2}})\s*до\s*(\d{{1,2}})\s*{_HOURS_WORD}\b", re.I)
_RE_HOURS_UPTO = re.compile(rf"\b(?:до|максимум|не\s*больше)\s*(\d{{1,2}})\s*{_HOURS_WORD}\b", re.I)
_RE_HOURS_FROM = re.compile(rf"\b(?:от|минимум|не\s*меньше)\s*(\d{{1,2}})\s*{_HOURS_WORD}\b", re.I)
_RE_HOURS_BARE = re.compile(rf"\b(\d{{1,2}})\s*{_HOURS_WORD}\b", re.I)

# Хвостовой \b здесь ставить нельзя: после «18+» стоит не-словесный символ,
# границы слова там нет и всё выражение молча перестаёт срабатывать.
_RE_UNDER18 = re.compile(r"\b(?:до\s*18|под\s*18|under\s*-?\s*18|школьник\w*|несовершеннолетн\w*)", re.I)
_RE_ADULT = re.compile(r"\b(?:18\s*\+|от\s*18\b|взросл\w*|совершеннолетн\w*|adult\b)", re.I)
# Человек чаще называет свой возраст, чем формулирует фильтр: «мне 20 лет»,
# «мне 20», «20 лет», «я 1998 года» — это тот же факт другими словами. Число
# без слова «лет» берём только после «мне/мне уже», иначе «нетто 18» стало бы
# возрастом вместо номера магазина.
_RE_MY_AGE = re.compile(
    r"\b(?:мне|мне\s+уже|я)\s+(\d{1,2})\s*(?:лет|года|год|годика)?\b"
    r"|\b(\d{1,2})\s*(?:лет|года|год|годика)\b",
    re.I,
)

_FULL_TIME = ("полная", "полный", "фуллтайм", "fuldtid", "fulltime", "full-time")
_PART_TIME = ("частичная", "подработка", "подработку", "неполная", "deltid",
              "parttime", "part-time", "студенческая")


@dataclass(frozen=True)
class Query:
    """Что человек имел в виду. Пустые поля означают «не сказал»."""

    raw: str = ""
    brands: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    employment: str = ""
    age: str = ""                 # "" | "under18" | "adult"
    hours_min: float | None = None
    hours_max: float | None = None
    terms: tuple[str, ...] = ()   # свободные слова + датские синонимы
    rest: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def empty(self) -> bool:
        return not (self.brands or self.cities or self.employment or self.age
                    or self.hours_min or self.hours_max or self.terms)

    @property
    def structured(self) -> bool:
        """Понял ли разбор хоть что-то кроме свободного текста."""
        return bool(self.brands or self.cities or self.employment or self.age
                    or self.hours_min is not None or self.hours_max is not None)


def _city_vocabulary(known_cities=None) -> dict[str, str]:
    """Словарь «как человек напишет» → «что искать в Job.city».

    Основа — русские алиасы и датские названия из labels. Плюс города самой
    базы, если их передал вызывающий: в Дании их сотни, вписывать все руками
    в словарь бессмысленно.
    """
    vocab: dict[str, str] = {}
    for alias, city in labels.CITY_ALIASES.items():
        vocab.setdefault(labels._fold(alias), city)
        vocab.setdefault(labels._fold(city), city)
    for group, members in labels.CITY_GROUPS.items():
        vocab.setdefault(labels._fold(group), group)
        for member in members:
            vocab.setdefault(labels._fold(member), member)
    for city in (known_cities or ()):
        text = str(city or "").strip()
        if text:
            vocab.setdefault(labels._fold(text), text)
    return vocab


def _known_profession(word: str) -> bool:
    """Знакомое слово о работе (есть в словаре RU→DA) — его не «исправляем»."""
    key = labels._fold(word)
    if key in ru_search.RU_DA:
        return True
    for danish in ru_search.RU_DA.values():
        if key in (labels._fold(item) for item in danish):
            return True
    return False


def fuzzy(word: str, vocabulary, *, min_len: int = FUZZY_MIN_LEN,
          cutoff: float = FUZZY_CUTOFF) -> str:
    """Ближайшее слово словаря или пусто. Короткие слова не трогаем."""
    key = labels._fold(word)
    if len(key) < min_len:
        return ""
    match = difflib.get_close_matches(key, list(vocabulary), n=1, cutoff=cutoff)
    return match[0] if match else ""


def _take_hours(text: str, notes: list[str]) -> tuple[str, float | None, float | None]:
    """Вынуть часы из фразы. Возвращает остаток текста и границы."""
    low = text
    match = _RE_HOURS_RANGE.search(low)
    if match:
        lo, hi = sorted((float(match.group(1)), float(match.group(2))))
        notes.append(f"часы: от {lo:g} до {hi:g} в неделю")
        return low[: match.start()] + " " + low[match.end():], lo, hi
    match = _RE_HOURS_UPTO.search(low)
    if match:
        hi = float(match.group(1))
        notes.append(f"часы: не больше {hi:g} в неделю")
        return low[: match.start()] + " " + low[match.end():], None, hi
    match = _RE_HOURS_FROM.search(low)
    if match:
        lo = float(match.group(1))
        notes.append(f"часы: от {lo:g} в неделю")
        return low[: match.start()] + " " + low[match.end():], lo, None
    match = _RE_HOURS_BARE.search(low)
    if match:
        value = float(match.group(1))
        lo = max(0.0, value - BARE_HOURS_SPREAD)
        hi = value + BARE_HOURS_SPREAD
        notes.append(
            f"часы: около {value:g} в неделю (беру от {lo:g} до {hi:g}; "
            f"напиши «до {value:g} часов» или «от {value:g} часов», если нужно точнее)"
        )
        return low[: match.start()] + " " + low[match.end():], lo, hi
    return low, None, None


def _take_age(text: str, notes: list[str]) -> tuple[str, str]:
    match = _RE_MY_AGE.search(text)
    if match:
        years = int(match.group(1) or match.group(2))
        if 10 <= years <= 99:
            rest = text[: match.start()] + " " + text[match.end():]
            if years < 18:
                # Подростку открыты и «детские», и обычные ставки, поэтому его
                # возраст ничего не отсекает — он лишь снимает вопрос.
                notes.append(f"тебе {years} — показываю и обычные вакансии, и «до 18»")
                return rest, ""
            notes.append(f"тебе {years} — без вакансий «только до 18 лет»")
            return rest, "adult"
    match = _RE_UNDER18.search(text)
    if match:
        notes.append("только вакансии для тех, кому нет 18")
        return text[: match.start()] + " " + text[match.end():], "under18"
    match = _RE_ADULT.search(text)
    if match:
        notes.append("без вакансий «только до 18 лет»")
        return text[: match.start()] + " " + text[match.end():], "adult"
    return text, ""


def stated_age(text: str) -> int | None:
    """Возраст, который человек назвал прямо. None — не называл.

    Отдельно от :func:`parse`, потому что это факт о человеке, а не фильтр
    одного запроса: приложение вправе запомнить его насовсем.
    """
    match = _RE_MY_AGE.search(" ".join(str(text or "").split()))
    if not match:
        return None
    years = int(match.group(1) or match.group(2))
    return years if 10 <= years <= 99 else None


def parse(text: str, *, exact: bool = False, known_cities=None) -> Query:
    """Разобрать запрос. `exact=True` — искать фразу буквально, как раньше."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return Query()
    if exact:
        return Query(raw=raw, terms=(raw,), rest=raw,
                     notes=("ищу фразу буквально, без разбора",))

    notes: list[str] = []
    rest, hours_min, hours_max = _take_hours(raw, notes)
    rest, age = _take_age(rest, notes)

    # Магазин: labels уже умеет и «jem og fix» из трёх слов, и «Netto Herlev».
    brands, rest = labels.split_brand_query(rest)
    named_brands = list(brands)
    if named_brands:
        notes.append("магазин: " + ", ".join(labels.source(b) for b in named_brands))

    vocab = _city_vocabulary(known_cities)
    cities: list[str] = []
    leftovers: list[str] = []
    words = [word.strip(",.;:!?()") for word in rest.split()]
    words = [word for word in words if word]
    max_city_words = max((len(key.split()) for key in vocab), default=1)
    index = 0
    while index < len(words):
        matched_city = ""
        matched_words = 0
        # Prefer the longest known name: «Kongens Lyngby» must not become the
        # free term «Kongens» plus a city (or two unrelated free terms).
        for width in range(min(max_city_words, len(words) - index), 0, -1):
            key = labels._fold(" ".join(words[index:index + width]))
            if key in vocab:
                matched_city = vocab[key]
                matched_words = width
                break
        if matched_city:
            if matched_city not in cities:
                cities.append(matched_city)
            index += matched_words
            continue
        leftovers.append(words[index])
        index += 1

    # Занятость — только явные слова, иначе легко сузить лишнего.
    employment = ""
    kept: list[str] = []
    for word in leftovers:
        key = labels._fold(word)
        if key in _FULL_TIME and not employment:
            employment = "fullTime"
            notes.append("занятость: полная")
        elif key in _PART_TIME and not employment:
            employment = "partTime"
            notes.append("занятость: частичная")
        else:
            kept.append(word)
    leftovers = kept

    # Опечатки. Трогаем только слово, которое не опознано ничем: ни магазин,
    # ни город, ни известная профессия. «Кассир» правке не подлежит — это
    # осмысленное слово, а не промах по клавише.
    corrected: set[str] = set()   # что уже названо в заметке про опечатку
    if leftovers:
        fixed: list[str] = []
        for word in leftovers:
            if _known_profession(word):
                fixed.append(word)
                continue
            guess = fuzzy(word, labels.BRAND_ALIASES)
            if guess:
                code = labels.BRAND_ALIASES[guess]
                if code not in brands:
                    brands = [*brands, code]
                corrected.add(code)
                notes.append(f"«{word}» → магазин {labels.source(code)} (исправил опечатку)")
                continue
            guess = fuzzy(word, vocab)
            if guess:
                city = vocab[guess]
                if city not in cities:
                    cities.append(city)
                corrected.add(city)
                notes.append(f"«{word}» → город {city} (исправил опечатку)")
                continue
            fixed.append(word)
        leftovers = fixed

    tail = " ".join(leftovers)
    terms = tuple(ru_search.expand(tail)) if tail else ()
    if terms and (len(terms) > 1 or terms[0].lower() != tail.lower()):
        extra = [t for t in terms if t.lower() != tail.lower()]
        if extra:
            notes.append("искал ещё по датским словам: " + ", ".join(extra[:6]))

    # Город раскрываем в агломерацию (Копенгаген = Valby, Herlev, …) на этапе
    # условий, а не здесь: в notes человеку честнее показать то, что он назвал.
    # Про исправленную опечатку уже сказано выше — не повторяемся.
    plain_cities = [c for c in cities if c not in corrected]
    if plain_cities:
        notes.append("город: " + ", ".join(plain_cities))

    return Query(raw=raw, brands=tuple(brands), cities=tuple(cities),
                 employment=employment, age=age,
                 hours_min=hours_min, hours_max=hours_max,
                 terms=terms, rest=tail, notes=tuple(notes))


def _escape_like(value: str) -> str:
    """Escape a literal fragment for a SQL LIKE/ILIKE pattern."""
    return str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clauses(query: Query) -> list:
    """Условия для `select(Job).where(*clauses(q))`. Часы и возраст — не здесь."""
    out = []
    if query.brands:
        cond = None
        for term in query.brands:
            part = Job.brand.ilike(f"%{_escape_like(term)}%", escape="\\")
            cond = part if cond is None else (cond | part)
        out.append(cond)
    if query.cities:
        cond = None
        for city in query.cities:
            for term in labels.city_terms(city) or [city]:
                part = Job.city.ilike(
                    f"%{_escape_like(term.strip())}%", escape="\\"
                )
                cond = part if cond is None else (cond | part)
        out.append(cond)
    if query.employment:
        out.append(Job.employment_type == query.employment)
    if query.terms:
        cond = None
        for term in query.terms:
            like = f"%{_escape_like(term)}%"
            part = (Job.title.ilike(like, escape="\\")
                    | Job.description.ilike(like, escape="\\")
                    | Job.city.ilike(like, escape="\\")
                    | Job.street.ilike(like, escape="\\"))
            cond = part if cond is None else (cond | part)
        out.append(cond)
    return out


def python_filter(query: Query, jobs) -> tuple[list, dict]:
    """Часы и возраст: их не проверить в SQL — часы лежат строкой «15-20».

    Возвращает отфильтрованный список и счётчики отсеянного по причинам,
    чтобы интерфейс мог сказать, что именно убрал, а не молча укоротить список.
    """
    if query.hours_min is None and query.hours_max is None and not query.age:
        return list(jobs), {}
    import autopilot  # локально: query_parse не должен тянуть автопилот при импорте

    kept, dropped = [], {"hours": 0, "age": 0}
    for job in jobs:
        if query.age:
            under = autopilot.job_is_under18(job)
            if (query.age == "under18") != under:
                dropped["age"] += 1
                continue
        if query.hours_min is not None or query.hours_max is not None:
            hours = autopilot._job_hours(job)
            # Часы неизвестны — не отбрасываем: молчание вакансии не ответ.
            if hours is not None:
                if query.hours_min is not None and hours < query.hours_min:
                    dropped["hours"] += 1
                    continue
                if query.hours_max is not None and hours > query.hours_max:
                    dropped["hours"] += 1
                    continue
        kept.append(job)
    return kept, {key: value for key, value in dropped.items() if value}


def describe(query: Query) -> str:
    """Строка «Понял так: …» для интерфейса. Пусто — разбирать было нечего."""
    return " · ".join(query.notes)
