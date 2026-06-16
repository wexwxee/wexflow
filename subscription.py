"""Подписка WexFlow — ЗАГОТОВКА (без реальной оплаты).

Цель этого модуля — заранее подготовить «систему» подписки, чтобы позже
подключить реальную оплату (облачный лицензионный сервер + Stripe + активация)
без переписывания приложения. Сейчас:

- состояние хранится локально в одном общем JSON (config.LICENSE_PATH);
- по умолчанию план «free», ничего не блокируется;
- есть единая точка проверки plan()/is_pro()/is_max() и карта тарифов PLANS —
  на будущее, когда подключим оплату. Сейчас feature_enabled() возвращает True
  для всех (гейтинг ВЫКЛЮЧЕН), чтобы поведение приложения не менялось.

Тарифная лесенка (по фазам автопилота):
- free «Сам»      — поиск, фильтры, ручная подача;
- pro  «Помощник» — автопоиск + автоподготовка анкет (отправляешь сам);
- max  «Автопилот»— полная автоотправка + все модули/коннекторы + ИИ.

Цены ещё не финализированы — поле price=None, валюта евро (€).
"""
import json

import config

CURRENCY = "€"

# Тарифы по порядку возрастания. order — для сравнения «доступности».
PLANS = {
    "free": {
        "order": 0,
        "name": "Free",
        "tagline": "Поиск вручную",
        "icon": "cart",
        "recommended": False,
        "price": None,            # 0 — бесплатно
        "summary": "Всё для самостоятельного поиска и отклика — бесплатно и без ограничений.",
        "features": [
            "Все вакансии Salling в одной ленте",
            "Умные фильтры: город, расстояние, часы, зарплата, бренд",
            "Время в пути и маршрут от дома до работы",
            "Описания вакансий на русском",
            "Отклик в один клик и хранение профиля с CV",
        ],
    },
    "pro": {
        "order": 1,
        "name": "Pro",
        "tagline": "Автопоиск",
        "icon": "zap",
        "recommended": True,
        "price": None,            # цена позже, в €
        "summary": "Автопилот сам ищет и заполняет анкеты — тебе остаётся подтвердить.",
        "features": [
            "Всё из Free",
            "Автопилот следит за вакансиями 24/7 в фоне",
            "Гибкие правила: радиус, часы, свежесть, бренд",
            "Мгновенные уведомления о подходящих",
            "Автозаполнение анкеты — остаётся нажать «Отправить»",
        ],
    },
    "max": {
        "order": 2,
        "name": "Max",
        "tagline": "Полный автопилот",
        "icon": "star",
        "recommended": False,
        "price": None,            # цена позже, в €
        "summary": "Бот откликается сам и на всех площадках, пока ты занят своими делами.",
        "features": [
            "Всё из Pro",
            "Автоотклик без твоего участия",
            "Все площадки: Salling, 7-Eleven и коннекторы",
            "Повышенные дневные лимиты и приоритет очереди",
            "ИИ-письмо под каждую вакансию и перевод резюме",
        ],
    },
}

PLAN_ORDER = ["free", "pro", "max"]

# Сравнение тарифов: строки = функции, значения = с какого тарифа доступно.
# Для таблицы «Free / Pro / Max» (✓ или —).
COMPARISON = [
    ("Лента вакансий и фильтры", "free"),
    ("Маршрут и расстояние до дома", "free"),
    ("Перевод описаний на русский", "free"),
    ("Отклик вручную", "free"),
    ("Профиль и документы", "free"),
    ("Автопоиск в фоне 24/7", "pro"),
    ("Гибкие правила подбора", "pro"),
    ("Уведомления о совпадениях", "pro"),
    ("Автозаполнение анкет", "pro"),
    ("Автоотклик без участия", "max"),
    ("Все площадки и коннекторы", "max"),
    ("Повышенные лимиты и приоритет", "max"),
    ("ИИ-письмо и перевод резюме", "max"),
]

# К какому МИНИМАЛЬНОМУ тарифу относится функция (для будущего гейтинга).
FEATURE_MIN_PLAN = {
    "manual_apply": "free",
    "autopilot": "pro",          # автопоиск + подготовка
    "auto_submit": "max",        # полная автоотправка
    "seven_eleven": "max",
    "connectors": "max",
    "ai_cover": "max",
}

# Включён ли реальный гейтинг функций. Пока False: все функции доступны всем,
# подписка — только витрина. Когда подключим оплату, переключим в True.
ENFORCE = False

DEFAULT = {
    "plan": "free",       # free | pro | max
    "active": False,      # активна ли платная подписка
    "since": None,        # ISO-дата начала (на будущее)
    "until": None,        # ISO-дата окончания (на будущее)
    "source": "stub",     # источник статуса: stub | manual | server
}


def load() -> dict:
    """Текущее состояние подписки (с подстановкой значений по умолчанию)."""
    data = dict(DEFAULT)
    try:
        if config.LICENSE_PATH.exists():
            saved = json.loads(config.LICENSE_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: saved.get(k, data[k]) for k in DEFAULT})
    except (OSError, ValueError):
        pass
    if data.get("plan") not in PLANS:
        data["plan"] = "free"
    return data


def save(data: dict) -> None:
    merged = dict(DEFAULT)
    merged.update({k: data.get(k, merged[k]) for k in DEFAULT})
    try:
        config.LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.LICENSE_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def plan() -> str:
    """Текущий активный тариф. Без активной подписки — всегда free."""
    data = load()
    p = data.get("plan", "free")
    if p == "free":
        return "free"
    return p if data.get("active") else "free"


def _order(p: str) -> int:
    return PLANS.get(p, PLANS["free"])["order"]


def is_pro() -> bool:
    """Тариф не ниже Pro (Pro или Max)."""
    return _order(plan()) >= _order("pro")


def is_max() -> bool:
    return _order(plan()) >= _order("max")


def feature_enabled(name: str) -> bool:
    """Доступна ли функция. Пока гейтинг ВЫКЛЮЧЕН (ENFORCE=False) — всё доступно.

    Когда подключим оплату, выставим ENFORCE=True и функции станут требовать
    нужного тарифа (FEATURE_MIN_PLAN). Сейчас точка вызова готова заранее.
    """
    if not ENFORCE:
        return True
    need = FEATURE_MIN_PLAN.get(name)
    if need is None:
        return True
    return _order(plan()) >= _order(need)


def set_plan(p: str, *, active: bool | None = None, source: str = "manual") -> dict:
    """Ручное переключение тарифа (для тестов/разработки). Реальной оплаты нет."""
    data = load()
    data["plan"] = p if p in PLANS else "free"
    data["active"] = (data["plan"] != "free") if active is None else bool(active)
    data["source"] = source
    save(data)
    return data


def _price_label(p: dict) -> str:
    if p["order"] == 0:
        return "Бесплатно"
    if p["price"] is None:
        return "— "  # цена ещё не задана
    return f"{p['price']} {CURRENCY}"


def status() -> dict:
    """Данные для шаблона страницы подписки."""
    current = plan()
    plans = []
    for key in PLAN_ORDER:
        p = PLANS[key]
        plans.append({
            "key": key,
            "name": p["name"],
            "tagline": p["tagline"],
            "icon": p["icon"],
            "recommended": p["recommended"],
            "summary": p["summary"],
            "features": p["features"],
            "price_label": _price_label(p),
            "is_free": key == "free",
            "is_current": key == current,
        })
    comparison = []
    for label, need in COMPARISON:
        need_order = _order(need)
        comparison.append({
            "label": label,
            "free": _order("free") >= need_order,
            "pro": _order("pro") >= need_order,
            "max": _order("max") >= need_order,
        })
    return {
        "plan": current,
        "current_name": PLANS[current]["name"],
        "is_pro": is_pro(),
        "is_max": is_max(),
        "currency": CURRENCY,
        "enforce": ENFORCE,
        "plans": plans,
        "comparison": comparison,
    }


def add_waitlist(email: str, plan: str) -> bool:
    """Сохраняет интерес к тарифу (вейтлист) в общий JSON. Заготовка под оплату."""
    email = (email or "").strip()
    if "@" not in email:
        return False
    plan = plan if plan in PLANS else "pro"
    path = config.SHARED_DIR / "waitlist.json"
    try:
        entries = []
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                entries = saved
        # не плодим дубли по (email, plan)
        if not any(e.get("email") == email and e.get("plan") == plan for e in entries):
            entries.append({"email": email, "plan": plan})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except (OSError, ValueError):
        return False
