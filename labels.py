"""Двуязычные подписи (русский + оригинал) и разбор ввода для фильтров."""
import re
import unicodedata

# Бренды — это имена, показываем красивый оригинал.
BRANDS = {
    "netto": "Netto", "foetex": "Føtex", "bilka": "Bilka", "br": "BR",
    "salling": "Salling", "carlsjr": "Carl's Jr", "starbucks": "Starbucks",
    "sallinggroup": "Salling Group", "hugoboss": "Hugo Boss", "matinique": "Matinique",
}

EMPLOYMENT = {
    "fullTime": "Полная занятость",
    "partTime": "Частичная занятость",
}

LEVEL = {
    "apprentice": "Стажёр / ученик",
    "employee": "Сотрудник",
    "employeeUnder18": "Сотрудник до 18 лет",
    "manager": "Менеджер",
}

# Поле job_level из данных Salling недостоверно: руководящие должности
# (Souschef, Serviceleder, Driftsleder, Teamkoordinator …) приходят с
# job_level="employee". Поэтому уровень «руководитель» определяем ещё и по
# названию — по слову. Одно правило ловит все составные варианты:
# souschef, serviceleder, salgsleder, teamkoordinator, projektchef, …
# Границы слова (\b…\b) важны, иначе зацепит salgsassistent/medarbejder.
# Помимо датских основ ловим английские руководящие (Store Manager, Supervisor,
# Team Lead, Head of…, Director, Chief, Foreman) — у Salling встречаются
# англоязычные вакансии, и автопилот не должен авто-подавать на руководящие.
# Намеренно «ловим с запасом»: ложно помеченная рядовая вакансия лишь не уйдёт
# в АВТО-подачу (можно подать вручную), а пропущенная руководящая — ушла бы сама.
_LEADERSHIP_RX = re.compile(
    r"\b\w*(?:chef|leder|koordinator|ansvarlig)\b"              # датские
    r"|\bdirekt\w*"                                             # датское: direktør
    r"|\b(?:manager|supervisor|director|chief|foreman|head)\b"  # английские
    r"|\blead(?:er)?\b",                                        # lead / leader / team lead
    re.IGNORECASE,
)


def is_leadership(title: str) -> bool:
    """True, если название должности — руководящее (по слову).

    Нужно как страховка поверх job_level: Salling часто метит руководящие
    роли как «employee». Ловит датские (souschef, serviceleder, teamkoordinator,
    …ansvarlig, direktør) и английские (Store Manager, Supervisor, Team Lead,
    Head of…, Director, Chief, Foreman) названия и не задевает рядовые
    (salgsassistent, kasseassistent, 1. assistent, morgenopfylder, cashier,
    sales assistant …). Проверка инварианта — tests/test_is_leadership.py."""
    return bool(_LEADERSHIP_RX.search(title or ""))

REGION = {
    # Дания
    "hovedstaden": "Столичный регион", "midtjylland": "Центральная Ютландия",
    "nordjylland": "Северная Ютландия", "sjaelland": "Зеландия",
    "syddanmark": "Южная Дания",
    # Германия
    "berlin": "Берлин", "brandenburg": "Бранденбург",
    "mecklenburgVorpommern": "Мекленбург-Передняя Померания",
    "sachsen": "Саксония", "sachsenAnhalt": "Саксония-Анхальт",
    "schleswigHolstein": "Шлезвиг-Гольштейн",
    # Польша
    "dolnoslaskie": "Нижнесилезское", "kujawskoPomorskie": "Куявско-Поморское",
    "lodzkie": "Лодзинское", "lubelskie": "Люблинское", "lubuskie": "Любушское",
    "malopolskie": "Малопольское", "mazowieckie": "Мазовецкое",
    "opolskie": "Опольское", "podkarpackie": "Подкарпатское",
    "pomorskie": "Поморское", "slaskie": "Силезское",
    "swietokrzyskie": "Свентокшиское", "warminskoMazurskie": "Варминьско-Мазурское",
    "wielkopolskie": "Великопольское", "zachodniopomorskie": "Западнопоморское",
}

CATEGORY = {
    "administration": "Администрация", "baker": "Пекарь",
    "businessDevelopment": "Развитие бизнеса", "butcher": "Мясник",
    "cafeRestaurant": "Кафе / Ресторан", "cashier": "Кассир",
    "customerService": "Обслуживание клиентов", "customerServices": "Обслуживание клиентов",
    "distributionAndWarehouse": "Склад / Дистрибуция", "finance": "Финансы",
    "humanResources": "HR / Персонал", "inventory": "Учёт запасов", "it": "IT",
    "logisticsSupplyChain": "Логистика", "marketing": "Маркетинг",
    "procurementAndPurchasingGrocery": "Закупки: продукты",
    "procurementAndPurchasingNonFood": "Закупки: нон-фуд",
    "procurementAndPurchasingTextile": "Закупки: текстиль",
    "salesElectronics": "Продажи: электроника", "salesFood": "Продажи: продукты",
    "salesFruitVegetables": "Продажи: фрукты/овощи", "salesGeneral": "Продажи: общее",
    "salesHouseGarden": "Продажи: дом/сад", "salesNearfood": "Продажи: nearfood",
    "salesNonfood": "Продажи: нон-фуд", "salesOperations": "Продажи: операции",
    "salesTextile": "Продажи: текстиль",
    "warehouseGoodsHandling": "Склад: обработка товаров",
}

# Частые города Salling/Salling Group и варианты, которые удобно вводить по-русски.
# Значение — датская/оригинальная строка, которую реально ищем в поле city.
CITY_ALIASES = {
    "копенгаген": "København",
    "кобенгавн": "København",
    "кобенхавн": "København",
    "кёбенхавн": "København",
    "copenhagen": "København",
    "kobenhavn": "København",
    "københavn": "København",
    "фредериксберг": "Frederiksberg",
    "frederiksberg": "Frederiksberg",
    "броннбю": "Brøndby",
    "брённбю": "Brøndby",
    "brondby": "Brøndby",
    "brøndby": "Brøndby",
    "бронсхой": "Brønshøj",
    "брёнсхой": "Brønshøj",
    "броншой": "Brønshøj",
    "brønshøj": "Brønshøj",
    "bronshoj": "Brønshøj",
    "хвидовре": "Hvidovre",
    "видовре": "Hvidovre",
    "hvidovre": "Hvidovre",
    "родовре": "Rødovre",
    "рёдовре": "Rødovre",
    "rodovre": "Rødovre",
    "rødovre": "Rødovre",
    "валби": "Valby",
    "вальбю": "Valby",
    "valby": "Valby",
    "ванлесе": "Vanløse",
    "ванлёсе": "Vanløse",
    "vanløse": "Vanløse",
    "vanlose": "Vanløse",
    "глоструп": "Glostrup",
    "glostrup": "Glostrup",
    "тааструп": "Taastrup",
    "тосструп": "Taastrup",
    "taastrup": "Taastrup",
    "орхус": "Aarhus",
    "aarhus": "Aarhus",
    "arhus": "Aarhus",
    "århus": "Aarhus",
    "оденсе": "Odense",
    "odense": "Odense",
    "ольборг": "Aalborg",
    "олборг": "Aalborg",
    "aalborg": "Aalborg",
    "эсбьерг": "Esbjerg",
    "эсбьорг": "Esbjerg",
    "esbjerg": "Esbjerg",
    "рандэрс": "Randers",
    "раннерс": "Randers",
    "randers": "Randers",
    "колдинг": "Kolding",
    "kolding": "Kolding",
    "вейле": "Vejle",
    "вайле": "Vejle",
    "vejle": "Vejle",
    "роскилле": "Roskilde",
    "roskilde": "Roskilde",
    "кеге": "Køge",
    "кёге": "Køge",
    "koge": "Køge",
    "køge": "Køge",
    "гриве": "Greve",
    "греве": "Greve",
    "greve": "Greve",
    "ишой": "Ishøj",
    "исхой": "Ishøj",
    "ishoj": "Ishøj",
    "ishøj": "Ishøj",
    "люнгбю": "Kgs. Lyngby",
    "лингби": "Kgs. Lyngby",
    "lyngby": "Kgs. Lyngby",
    "kgs lyngby": "Kgs. Lyngby",
    "хернинг": "Herning",
    "herning": "Herning",
    "хорсенс": "Horsens",
    "horsens": "Horsens",
    "силькеборг": "Silkeborg",
    "silkeborg": "Silkeborg",
    "хиллерод": "Hillerød",
    "хиллерёд": "Hillerød",
    "hillerod": "Hillerød",
    "hillerød": "Hillerød",
    "хельсингер": "Helsingør",
    "хельсингёр": "Helsingør",
    "helsingor": "Helsingør",
    "helsingør": "Helsingør",
    "нествед": "Næstved",
    "нэствед": "Næstved",
    "naestved": "Næstved",
    "næstved": "Næstved",
    "слегельсе": "Slagelse",
    "слагельсе": "Slagelse",
    "slagelse": "Slagelse",
    "хольбек": "Holbæk",
    "хольбэк": "Holbæk",
    "holbaek": "Holbæk",
    "holbæk": "Holbæk",
    "свеннборг": "Svendborg",
    "свенборг": "Svendborg",
    "svendborg": "Svendborg",
    "соннерборг": "Sønderborg",
    "сённерборг": "Sønderborg",
    "sonderborg": "Sønderborg",
    "sønderborg": "Sønderborg",
    "виборг": "Viborg",
    "viborg": "Viborg",
    "херлев": "Herlev",
    "herlev": "Herlev",
}

CITY_GROUPS = {
    "København": [
        "København", "København K", "København N", "København S", "København V",
        "København Ø", "København NV", "København SV",
        "Brønshøj", "Valby", "Vanløse", "Frederiksberg", "Frederiksberg C",
        "Hvidovre", "Rødovre", "Glostrup", "Herlev", "Ballerup", "Kastrup",
        "Tårnby", "Hellerup", "Gentofte", "Kongens Lyngby", "Kgs. Lyngby",
    ],
    "Aarhus": [
        "Aarhus", "Aarhus C", "Aarhus N", "Aarhus V",
        "Tilst", "Højbjerg", "Viby J", "Risskov", "Brabrand", "Åbyhøj", "Egå",
    ],
    "Odense": [
        "Odense", "Odense C", "Odense M", "Odense N", "Odense NV",
        "Odense S", "Odense SØ", "Odense SV",
    ],
    "Aalborg": [
        "Aalborg", "Aalborg SV", "Aalborg Øst", "Nørresundby",
    ],
}


def _fold(text: str) -> str:
    """Нормализует строку для мягкого сравнения: регистр, ё/е, диакритика."""
    text = (text or "").strip().lower().replace("ё", "е")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text)


def role_summary(title: str, categories: str = "", job_level: str = "") -> str:
    """Коротко объясняет по-русски, на кого вакансия, сохраняя оригинал отдельно.

    Это намеренно не машинный перевод всего заголовка: компактная классификация
    мгновенна, стабильна и хорошо сканируется в длинной ленте.
    """
    raw = str(title or "").lower()
    text = _fold(title).replace("æ", "ae").replace("ø", "o")
    hay = f"{raw} {text}"
    rules = (
        (r"studentermedhj|studentermedarbejder|student assistant|working student", "Студент-помощник"),
        (r"franchisetager|franchisee", "Франчайзи / управляющий магазином"),
        (r"ungarbejder|young worker", "Молодой сотрудник"),
        (r"søg uopfordret|sog uopfordret|unsolicited", "Открытая заявка"),
        (r"click\s*&\s*collect.*(?:leder|lead|manager)", "Руководитель Click & Collect"),
        (r"full[ -]?stack", "Full-stack разработчик"),
        (r"backend|back-end", "Backend-разработчик"),
        (r"frontend|front-end", "Frontend-разработчик"),
        (r"cloud engineer|cloud udvikler", "Облачный инженер"),
        (r"data engineer|analytics engineer", "Инженер данных"),
        (r"mlops|machine learning", "ML / MLOps инженер"),
        (r"software|udvikler|developer|app-udvikler", "Разработчик ПО"),
        (r"security|compliance|cyber", "Информационная безопасность"),
        (r"project controller|financial controller|controller", "Финансовый контролёр"),
        (r"projektøkonomi|projektokonomi|project finance", "Проектные финансы"),
        (r"forretningsanalytiker|business analyst|data analyst|analytiker|analyst", "Аналитик"),
        (r"account executive|account manager|customer success", "Работа с клиентами"),
        (r"marketing|content specialist|kommunikation", "Маркетинг / контент"),
        (r"sælger|saelger|salgsassistent|sales assistant|butiksassistent", "Продавец-консультант"),
        (r"kitchen|køkken|kok\b|cook\b", "Кухня / повар"),
        (r"lager|warehouse|logistik", "Склад / логистика"),
        (r"support|kundeservice|customer service", "Поддержка клиентов"),
        (r"rådgiver|raadgiver|consultant|konsulent", "Консультант"),
        (r"koordinator|coordinator", "Координатор"),
        (r"recruiter|rekruttering|talent acquisition|human resources|\bhr\b", "Подбор персонала / HR"),
        (r"regnskab|accountant|bogholder", "Бухгалтерия"),
        (r"indkøb|indkob|procurement|purchasing", "Закупки"),
        (r"jurist|legal counsel|lawyer", "Юрист"),
        (r"designer|ux\b|ui\b", "Дизайнер"),
        (r"tekniker|technician", "Технический специалист"),
        (r"chauffør|chauffor|driver\b", "Водитель"),
        (r"assistant|assistent", "Ассистент"),
        (r"specialist", "Специалист"),
        (r"ingeniør|ingenior|engineer", "Инженер"),
        (r"leder|manager|head of|chef\b|director", "Руководитель"),
        (r"medarbejder|employee|worker", "Сотрудник"),
    )
    for pattern, summary in rules:
        if re.search(pattern, hay, re.I):
            return summary
    for code in str(categories or "").split(","):
        code = code.strip()
        if code and CATEGORY.get(code):
            return CATEGORY[code]
    if job_level and LEVEL.get(job_level):
        return LEVEL[job_level]
    return ""


def date_short(value) -> str:
    """ISO-дата в привычном русском виде; неизвестный формат не ломаем."""
    raw = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    return f"{match.group(3)}.{match.group(2)}.{match.group(1)}" if match else raw[:10]


def bi(mapping: dict, key: str) -> str:
    """Подпись 'Русский · оригинал'. Если перевода нет — только оригинал."""
    ru = mapping.get(key)
    return f"{ru} · {key}" if ru else key


def brand(key: str) -> str:
    return BRANDS.get(key, key.title())


def human(mapping: dict, key: str) -> str:
    """Человеческая подпись для select: русский (оригинал)."""
    ru = mapping.get(key)
    return f"{ru} ({key})" if ru else key


def with_count(label: str, count: int | None = None) -> str:
    if count is None:
        return label
    return f"{label} — {count}"


def plural(value: int | str | None, one: str, few: str, many: str) -> str:
    """Возвращает русскую форму слова для числа: 1, 2–4 или остальные."""
    try:
        number = abs(int(value or 0))
    except (TypeError, ValueError):
        number = 0
    if 11 <= number % 100 <= 14:
        return many
    if number % 10 == 1:
        return one
    if 2 <= number % 10 <= 4:
        return few
    return many


def resolve(mapping: dict, text: str) -> str:
    """Превращает введённый пользователем текст (код / рус. название / часть) в код.
    Пусто — если ничего не подошло (фильтр тогда не применяется)."""
    if not text or not text.strip():
        return ""
    t = _fold(text)
    for code in mapping:                       # точное совпадение кода
        if _fold(code) == t:
            return code
    for code, name in mapping.items():         # подстрока в названии или коде
        if t in _fold(str(name)) or t in _fold(code):
            return code
    return ""


def city_query(text: str) -> str:
    """Возвращает город/часть города для поиска в БД.

    Примеры: Копенгаген -> København, Орхус -> Aarhus, брон -> Brøndby.
    Если алиас не найден, возвращает исходный ввод, чтобы частичный поиск продолжал работать.
    """
    if not text or not text.strip():
        return ""
    raw = text.strip()
    folded = _fold(raw)
    if folded in CITY_ALIASES:
        return CITY_ALIASES[folded]
    for alias, city in CITY_ALIASES.items():
        a = _fold(alias)
        if len(folded) >= 3 and (folded in a or a in folded):
            return city
    return raw


def city_terms(text: str) -> list[str]:
    """Список городов/районов для фильтра.

    Для больших городов возвращает агломерацию/районы, потому что Salling часто пишет
    не "København", а конкретный район: Brønshøj, Valby, Frederiksberg и т.д.
    """
    city = city_query(text)
    if not city:
        return []
    base = city.split()[0] if city in ("Aarhus C", "Aarhus N", "Aarhus V") else city
    for group_name, terms in CITY_GROUPS.items():
        if city == group_name or city in terms or base == group_name:
            return terms
    return [city]


def localize_address(text: str) -> str:
    """Подменяет русские названия городов в адресе на оригинальные перед геокодингом."""
    if not text or not text.strip():
        return ""
    out = text.strip()
    # Локализуем (русский город -> датский) ТОЛЬКО если во вводе есть кириллица.
    # Иначе латинское название улицы ломалось: подстрока города "lyngby" внутри
    # "Lyngbyvej" подменялась на "Kgs. Lyngby" -> несуществующий адрес. Датские
    # адреса (Lyngbyvej, Roskildevej, Frederiksberggade …) отдаём как есть.
    if not re.search(r"[А-Яа-яЁё]", out):
        return out
    folded = _fold(out)
    for alias, city in sorted(CITY_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        a = _fold(alias)
        if a and a in folded:
            out = re.sub(re.escape(alias), city, out, flags=re.I)
            if out == text.strip():  # алиас мог отличаться после folding/диакритики
                out = f"{out}, {city}" if city.lower() not in out.lower() else out
            break
    return out
