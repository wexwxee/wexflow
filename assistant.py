"""Помощник сбоку: понимает просьбу и дёргает готовые умения приложения.

Зачем именно так. Чат, который «сам всё знает», врёт: модель придумает
вакансию, адрес и часы, и человек поедет в несуществующий магазин. Поэтому
здесь помощник — **не рассказчик, а диспетчер**: он выбирает ИНСТРУМЕНТ из
короткого белого списка, инструмент выполняет приложение по своей базе, и
человеку показываются карточки из результата, а не текст модели.

Порядок появления тоже осознанный: сначала (этап 4) помощник работал
**вообще без ИИ** — просьбу разбирает `guess_tool` теми же словарями, что и
поиск. Это значит, что помощник есть у всех, включая людей без ключа и с
исчерпанной квотой. ИИ (этап 6) работает сверху и умеет ровно одно:
выбрать имя инструмента и аргументы. Ни одного факта из модели.

С 12.08.2026 у ИИ появилась вторая работа — **слова**. Раньше человек получал
одну и ту же заготовку на любой случай («Ничего не нашлось. Попробуй назвать
магазин или город»), и помощник справедливо казался тупым. Теперь, если ключ
подключён, ИИ формулирует ответ — но строго ПОВЕРХ данных инструмента: список
карточек, числа и статусы остаются те, что посчитало приложение, а модель лишь
пересказывает их по-человечески. Нет ключа, кончилась квота, сбой сети —
человек видит прежний детерминированный текст, и помощник продолжает работать.

Три границы, которые нельзя переносить:
  1. **Только белый список.** Неизвестное имя инструмента или кривой аргумент
     — вежливый отказ, а не попытка догадаться. Никакого SQL «из текста».
  2. **Подача — не инструмент помощника.** `prepare_application` возвращает
     карточку подтверждения со ссылкой; жмёт человек. Отправить заявку через
     помощника нельзя вообще — это закреплено тестом-инвариантом.
  3. **О человеке — только по делу.** Наружу (в будущий промпт ИИ) уходит
     узкий список полей: город, языки, опыт. Ни почты, ни телефона, ни адреса,
     ни даты рождения, ни содержимого CV — по образцу `connectors/ai_fill`.
  4. **Слова — можно, факты — нет.** ИИ переписывает только строку ответа и
     только из того, что вернул инструмент. Карточки вакансий он не создаёт и
     не меняет никогда, а карточку подтверждения подачи не трогает вовсе:
     обещание «кнопку жмёшь ты» должно звучать дословно.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

import labels

MAX_RESULTS = 8
MAX_QUERY = 120
MAX_REPLY = 600
MAX_HISTORY = 6                 # сколько реплик диалога помнит помощник
MAX_HISTORY_CHARS = 200
AI_ROUTER_TIMEOUT = 8.0
AI_REPLY_TIMEOUT = 9.0

# Что помощник вправе знать о человеке. Всё остальное — не его дело: адрес,
# телефон, почта, дата рождения и CV к поиску работы в ленте отношения не имеют.
CONTEXT_WHITELIST = (
    "first_name", "city", "zip", "country", "languages",
    "experience_years", "current_role", "available_from",
)


@dataclass(frozen=True)
class Tool:
    """Одно умение приложения, доступное помощнику."""

    name: str
    human: str                      # как это называется для человека
    args: dict                      # имя → ("str"|"int", ограничение)
    run: Callable[[dict], dict]


def _clean_args(tool: "Tool", args: dict) -> dict:
    """Привести аргументы к схеме. Лишнее выбрасываем, кривое — тоже."""
    clean: dict = {}
    for name, spec in tool.args.items():
        value = (args or {}).get(name)
        if value is None:
            continue
        kind = spec[0]
        if kind == "str":
            text = " ".join(str(value).split())[: spec[1]]
            if text:
                clean[name] = text
        elif kind == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            # Число вне диапазона выбрасываем, а не подтягиваем к границе:
            # «мне 3 года», записанное как 10, — это выдуманный факт о человеке.
            # Потерянный аргумент честно превращается в «не понял».
            if spec[1] <= number <= spec[2]:
                clean[name] = number
    return clean


# ── карточки для интерфейса ────────────────────────────────────────────────

def _hours_label(job) -> str:
    """«15» → «15 ч/нед», но «7 timer» оставляем как есть — иначе выйдет
    «7 timer ч/нед»: источники пишут часы по-разному."""
    import re

    hours = str(getattr(job, "hours", "") or "").strip()
    if not hours:
        return ""
    return f"{hours} ч/нед" if re.fullmatch(r"[\d.,\s–—-]+", hours) else hours


def job_card(job, *, why=None, distance=None) -> dict:
    """Одинаковая карточка вакансии для любого ответа помощника."""
    return {
        "kind": "job",
        "id": job.id,
        "title": str(job.title or ""),
        "brand": labels.brand(job.brand) if job.brand else "",
        "city": str(job.city or ""),
        "street": str(job.street or ""),
        "hours": _hours_label(job),
        "href": f"/job/{job.id}",
        "why": list(why or []),
        "distance": distance,
    }


def _visible_jobs(clauses, *, limit=MAX_RESULTS, extra=None):
    import feed
    from db import Job, get_session, select

    with get_session() as session:
        stmt = select(Job).where(
            *feed.visible_clauses(exclude_applied=True), *(clauses or [])
        )
        if extra is not None:
            stmt = stmt.where(extra)
        stmt = stmt.order_by(Job.published.desc())
        if limit is not None:
            stmt = stmt.limit(limit * 3)
        rows = list(session.exec(stmt).all())
    if feed.hide_barrier():
        import relevance
        rows = [j for j in rows if not relevance.is_barrier(j)]
    return rows[:limit] if limit is not None else rows


# ── сами инструменты ───────────────────────────────────────────────────────

def _tool_search(args: dict) -> dict:
    import query_parse
    from db import Job, get_session, select

    text = args.get("query", "")
    with get_session() as session:
        known = [row for row in session.exec(select(Job.city).distinct()).all() if row]
    parsed = query_parse.parse(text, known_cities=known)
    # Hours and age are stored in forms that only python_filter can interpret.
    # Do not cap the SQL candidates before that filter: the first 24 rows may
    # all fail while a valid result exists immediately after them.
    jobs = _visible_jobs(query_parse.clauses(parsed), limit=None)
    jobs, _dropped = query_parse.python_filter(parsed, jobs)
    jobs = jobs[:MAX_RESULTS]
    return {
        "ok": True,
        "kind": "jobs",
        "understood": query_parse.describe(parsed),
        "results": [job_card(job) for job in jobs],
        "empty_hint": "Ничего не нашлось. Попробуй назвать магазин или город.",
    }


def _tool_nearby(args: dict) -> dict:
    import feed
    import nearby
    import query_parse
    import settings_store
    from db import Job, get_session, select

    text = args.get("query", "")
    with get_session() as session:
        known = [row for row in session.exec(select(Job.city).distinct()).all() if row]
        parsed = query_parse.parse(text, known_cities=known)
        pool = list(session.exec(select(Job).where(
            *feed.visible_clauses(exclude_applied=True, fit=False)
        )).all())
    home = settings_store.get_home()
    if not parsed.brands and not parsed.cities:
        # «что есть рядом» без названия места — самый частый вопрос. Отвечаем
        # тем, что ближе всего к дому, а не отговоркой «назови магазин».
        return _closest_to_home(pool, home)
    view = nearby.suggestions(pool, parsed=parsed, home=home)
    if not view:
        return _closest_to_home(pool, home)
    cards = [{"kind": "store", "title": f"{labels.brand(g['brand'])} · {g['city'] or ''}".strip(" ·"),
              "subtitle": f"{g['street'] or ''}"
                          + (f" · {g['dist']} км" if g["dist"] is not None else "")
                          + f" · {len(g['jobs'])} "
                          + labels.plural(len(g['jobs']), 'вакансия', 'вакансии', 'вакансий'),
              "href": "/?q=" + f"{labels.brand(g['brand'])} {g['city'] or ''}".strip()}
             for g in view["same_brand"]]
    cards += [job_card(j) for j in view["same_role"][:3]]
    return {"ok": True, "kind": "cards", "results": cards,
            "reply": _nearby_words(view)}


def _closest_to_home(pool, home) -> dict:
    """Ближайшие к дому вакансии — ответ на «а что есть рядом» без места."""
    import feed
    import geo
    import relevance

    if not home:
        return {"ok": True, "kind": "text",
                "reply": "Чтобы показать ближнее, нужен домашний адрес — он задаётся в профиле.",
                "href": "/profile#home", "button": "Указать дом"}
    ranked = []
    for job in pool:
        if (job.lat is None or job.lon is None
                or (feed.hide_barrier() and relevance.is_barrier(job))):
            continue
        if str(getattr(job, "status", "") or "") in ("closed", "hidden", "applied"):
            continue
        ranked.append((round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon), 1), job))
    ranked.sort(key=lambda pair: pair[0])
    if not ranked:
        return {"ok": True, "kind": "text",
                "reply": "Рядом ничего не нашлось. Попробуй назвать магазин или город."}
    return {"ok": True, "kind": "jobs",
            "reply": "Вот что ближе всего к дому:",
            "results": [job_card(job, why=[f"{km:g} км от дома"], distance=km)
                        for km, job in ranked[:MAX_RESULTS]]}


def _nearby_words(view: dict) -> str:
    parts = []
    rejected = view.get("anchor_rejected") or {}
    if view.get("anchor_count") and rejected:
        why = []
        if rejected.get("under18"):
            why.append(f"{rejected['under18']} только для тех, кому нет 18")
        if rejected.get("leadership"):
            why.append(f"{rejected['leadership']} руководящих")
        if rejected.get("language"):
            why.append(f"{rejected['language']} с требованием датского")
        count = view["anchor_count"]
        word = labels.plural(count, "вакансия", "вакансии", "вакансий")
        parts.append(f"В {view['anchor_label']} сейчас {count} {word}, "
                     f"но подходящих нет: {', '.join(why)}.")
    if view.get("same_brand_count"):
        parts.append(f"Зато рядом — {view['same_brand_count']} в той же сети.")
    return " ".join(parts) or "Смотри, что нашлось рядом."


def _tool_recommend(args: dict) -> dict:
    import feed
    import recommend
    import settings_store
    from db import Job, get_session, select

    if not recommend.enabled():
        return {"ok": True, "kind": "text",
                "reply": "Рекомендации пока выключены. Включить их можно в профиле — "
                         "тогда я смогу ставить наверх то, что подходит именно тебе.",
                "href": "/profile#recommend", "button": "Открыть профиль"}
    with get_session() as session:
        jobs = list(session.exec(
            select(Job).where(*feed.visible_clauses(exclude_applied=True))
        ).all())
    if feed.hide_barrier():
        import relevance
        jobs = [job for job in jobs if not relevance.is_barrier(job)]
    ctx = recommend.build_context(home=settings_store.get_home())
    ranked = recommend.rank(jobs, ctx)[:MAX_RESULTS]
    return {"ok": True, "kind": "jobs",
            "results": [job_card(job, why=why) for job, _score, why in ranked],
            "reply": "Вот что подходит тебе больше всего — под каждой написано почему."}


def _tool_explain_verdict(args: dict) -> dict:
    import relevance
    from db import Job, get_session

    with get_session() as session:
        job = session.get(Job, args.get("job_id", ""))
    if job is None:
        return {"ok": False, "kind": "text", "reply": "Не нашёл такую вакансию."}
    view = relevance.describe(job)
    if view["verdict"] == relevance.UNCLEAR:
        text = "По этой вакансии вердикта нет: в тексте про язык ничего не сказано."
    elif view["soft"]:
        text = (f"{view['reason']} Так решил ИИ по названию должности — в объявлении "
                "требования датского нет, поэтому вакансию и не прячем.")
    elif view["barrier"]:
        text = f"{view['reason']} Это видно прямо в тексте вакансии или это руководящая должность."
    else:
        text = f"{view['reason'] or 'Похоже, возьмут без датского.'}"
    return {"ok": True, "kind": "text", "reply": text,
            "href": f"/job/{job.id}", "button": "Открыть вакансию"}


def _tool_job_facts(args: dict) -> dict:
    from db import Job, get_session

    with get_session() as session:
        job = session.get(Job, args.get("job_id", ""))
    if job is None:
        return {"ok": False, "kind": "text", "reply": "Не нашёл такую вакансию."}
    bits = [b for b in (
        labels.brand(job.brand) if job.brand else "",
        f"{job.street or ''} {job.zip or ''} {job.city or ''}".strip(),
        f"{job.hours} ч/нед" if job.hours else "",
        str(job.employment_type and labels.EMPLOYMENT.get(job.employment_type, "") or ""),
    ) if b]
    return {"ok": True, "kind": "text", "reply": " · ".join(bits) or "Подробностей нет.",
            "href": f"/job/{job.id}", "button": "Открыть вакансию"}


def _tool_profile_gaps(_args: dict) -> dict:
    import profile_store

    profile = profile_store.load_profile()
    required = {
        "Имя": profile.get("first_name"), "Фамилия": profile.get("last_name"),
        "Email": profile.get("email"), "Телефон": profile.get("phone"),
        "Адрес": profile.get("address"), "Индекс": profile.get("zip"),
        "Город": profile.get("city"), "Страна": profile.get("country"),
        "CV": profile.get("cv_path"),
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if not missing:
        return {"ok": True, "kind": "text", "reply": "Профиль заполнен — для подачи всего хватает."}
    return {"ok": True, "kind": "text",
            "reply": "Для подачи не хватает: " + ", ".join(missing) + ".",
            "href": "/profile", "button": "Заполнить профиль"}


def _tool_application_status(_args: dict) -> dict:
    import applications

    total = applications.submitted_total_count(source=None)
    today = applications.submitted_today_count(source=None)
    if not total:
        return {"ok": True, "kind": "text", "reply": "Поданных заявок пока нет.",
                "href": "/audit", "button": "Открыть журнал"}
    tail = f", из них сегодня {today}" if today else ""
    return {"ok": True, "kind": "text",
            "reply": f"Подано заявок: {total}{tail}. Что с каждой — в журнале.",
            "href": "/audit", "button": "Открыть журнал"}


def _tool_prepare_application(args: dict) -> dict:
    """НИКОГДА не отправляет. Возвращает карточку подтверждения со ссылкой.

    Это не осторожность ради осторожности: заявка уходит под настоящим именем
    человека, и отменить её нельзя. Решение о конкретной отправке принимает
    человек, а не фраза в чате.
    """
    import trust
    from db import Job, get_session

    with get_session() as session:
        job = session.get(Job, args.get("job_id", ""))
    if job is None:
        return {"ok": False, "kind": "text", "reply": "Не нашёл такую вакансию."}
    allowed, why = trust.auto_allowed(job.source or "")
    note = ("Площадка уже доказала подачу, но кнопку всё равно нажимаешь ты."
            if allowed else why)
    return {
        "ok": True,
        "kind": "confirm",
        "title": f"Открыть подготовку заявки: {job.title}?",
        "reply": ("WexFlow заполнит форму и остановится перед отправкой. "
                  f"Кнопку «Отправить» жмёшь ты. {note}"),
        "href": f"/job/{job.id}",
        "button": "Открыть вакансию",
    }


# Последняя правка профиля — чтобы «верни как было» работало сразу, без
# копания в файле руками. Живёт в памяти процесса: это удобство одного
# разговора, а не история изменений.
_last_profile_change: dict = {}


def _tool_update_profile(args: dict) -> dict:
    """Изменить поле профиля по просьбе человека.

    Мягкие поля меняем сразу и показываем «было → стало»: подтверждать каждую
    мелочь утомительно, а вернуть можно одной фразой. Личные и юридические поля
    не трогаем даже по прямой просьбе — см. модуль assistant_profile.
    """
    global _last_profile_change
    import assistant_profile

    field = str(args.get("field") or "").strip()
    value = args.get("value")
    if field not in assistant_profile.SOFT_FIELDS and field not in assistant_profile.HARD_FIELDS:
        field = assistant_profile.guess_field(f"{field} {value or ''}")
    if not field:
        return {"ok": False, "kind": "text",
                "reply": "Не понял, какое поле менять. Скажи, например: "
                         "«поставь город Копенгаген» или «я готов на вечерние смены»."}

    if field in assistant_profile.HARD_FIELDS:
        human = assistant_profile.HARD_FIELDS[field]
        return {
            "ok": True, "kind": "text",
            "reply": (f"Поле «{human}» я не меняю — оно уходит в настоящую анкету "
                      "под твоим именем, и опечатку там заметить поздно. "
                      "Открой профиль, там это поле видно целиком."),
            "href": "/profile", "button": "Открыть профиль",
        }

    result = assistant_profile.apply_change(field, value)
    if not result.get("ok"):
        human = assistant_profile.human_name(field)
        if result.get("reason") == "bad_value":
            return {"ok": False, "kind": "text",
                    "reply": f"Не смог записать «{human}»: {result['problem']}."}
        return {"ok": False, "kind": "text",
                "reply": f"Поле «{human}» менять не умею."}

    _last_profile_change = dict(result)
    was = assistant_profile.display(field, result["before"])
    now = assistant_profile.display(field, result["after"])
    return {
        "ok": True, "kind": "text",
        "reply": (f"Готово: «{result['human']}» — было {was}, стало {now}. "
                  "Скажи «верни как было», если это не то."),
        "href": "/profile", "button": "Посмотреть профиль",
    }


def _tool_undo_profile(_args: dict) -> dict:
    """Вернуть последнее изменение профиля, сделанное помощником."""
    global _last_profile_change
    import assistant_profile

    change = dict(_last_profile_change or {})
    if not change.get("field"):
        return {"ok": True, "kind": "text",
                "reply": "В этом разговоре я ничего в профиле не менял."}
    back = assistant_profile.apply_change(change["field"], change.get("before") or "")
    if not back.get("ok") and str(change.get("before") or "").strip():
        return {"ok": False, "kind": "text",
                "reply": "Не получилось вернуть. Открой профиль и поправь вручную.",
                "href": "/profile", "button": "Открыть профиль"}
    if not str(change.get("before") or "").strip():
        # Поле было пустым — возвращаем пустоту напрямую, минуя проверку значения.
        import profile_store
        profile_store.mutate_profile(
            lambda profile: profile.__setitem__(change["field"], "") or profile
        )
    _last_profile_change = {}
    human = assistant_profile.human_name(change["field"])
    was = assistant_profile.display(change["field"], change.get("before") or "")
    return {"ok": True, "kind": "text",
            "reply": f"Вернул: «{human}» снова {was}."}


# Разделы приложения, которые помощник вправе открыть. Белый список, как и всё
# остальное: адрес приходит отсюда, а не из текста модели.
PAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("/", "Вакансии", ("вакансии", "лента", "ленту", "список", "работу", "поиск")),
    ("/audit", "Журнал заявок", ("журнал", "мои заявки", "мои отклики", "аудит",
                                 "историю", "история")),
    ("/profile", "Профиль кандидата", ("профиль", "анкету", "анкета", "о себе",
                                       "мои данные")),
    ("/settings", "Настройки", ("настройки", "настройка", "параметры")),
    ("/settings/forms", "ИИ и анкеты", ("ии", "лимит", "ключ", "gemini", "groq",
                                        "claude")),
    ("/autopilot", "Автопилот", ("автопилот", "автоподача")),
    ("/apply-by-link", "Подача по ссылке", ("подача по ссылке", "по ссылке",
                                            "чужой сайт")),
    ("/settings/lidl", "Кабинет Lidl", ("lidl", "лидл")),
    ("/settings/salling", "Кабинет Salling", ("salling", "саллинг", "føtex",
                                              "netto", "нетто")),
    ("/help", "Помощь", ("помощь", "справка", "как пользоваться")),
)


def _tool_open_page(args: dict) -> dict:
    """Открыть раздел приложения.

    «Открой вакансии, хочу сам посмотреть» — это просьба перейти, а не искать.
    Раньше такая фраза уходила в поиск по тексту объявлений и возвращала
    «ничего не нашлось», хотя человек всего лишь просил кнопку.
    """
    wanted = " ".join(str(args.get("page") or args.get("query") or "").lower().split())
    best = None
    best_at = len(wanted) + 1
    for href, human, aliases in PAGES:
        for alias in aliases:
            at = wanted.find(alias)
            if at >= 0 and at < best_at:
                best, best_at = (href, human), at
    if best is None:
        names = ", ".join(human.lower() for _href, human, _a in PAGES)
        return {"ok": False, "kind": "text",
                "reply": f"Не понял, какой раздел открыть. Есть: {names}."}
    href, human = best
    return {"ok": True, "kind": "text", "reply": f"Открываю раздел «{human}».",
            "href": href, "button": human}


def _tool_help(_args: dict) -> dict:
    """Что помощник умеет. Список берётся из самого каталога, а не из текста.

    Без этого «привет» и «что ты умеешь» уходили в поиск по ленте и человек
    получал «ничего не нашлось» — худший первый ответ из возможных.
    """
    skills = [tool.human for tool in TOOLS.values() if tool.name != "help"]
    return {
        "ok": True, "kind": "text",
        "reply": ("Я ищу по твоей базе вакансий и объясняю, что в ней есть. "
                  "Умею: " + ", ".join(skills).lower() + ". "
                  "Заявку не отправляю — кнопку жмёшь ты."),
    }


def _tool_set_age(args: dict) -> dict:
    """Запомнить возраст человека и сразу сказать, что это меняет в ленте.

    Возраст — не поисковый запрос, а факт о человеке: почти треть датской
    розницы это ставки «under 18 år», куда совершеннолетнего не возьмут.
    Раньше фраза «мне 20 лет» уходила в поиск по тексту и возвращала
    «ничего не нашлось» — при том что менять надо было всю ленту.
    """
    import feed
    from db import Job, get_session, select

    try:
        years = int(args.get("age") or 0)
    except (TypeError, ValueError):
        years = 0
    if not 10 <= years <= 99:
        return {"ok": False, "kind": "text",
                "reply": "Не понял возраст. Напиши, например, «мне 20 лет»."}

    with get_session() as session:
        before = len(list(session.exec(select(Job.id).where(
            *feed.visible_clauses(exclude_applied=True)
        )).all()))
    feed.set_viewer_age(years)
    with get_session() as session:
        after = len(list(session.exec(select(Job.id).where(
            *feed.visible_clauses(exclude_applied=True)
        )).all()))

    hidden = max(0, before - after)
    if years < 18:
        reply = (f"Запомнил: тебе {years}. Показываю и обычные вакансии, "
                 "и те, что «under 18 år» — тебе открыты обе.")
    elif hidden:
        reply = (f"Запомнил: тебе {years}. Убрал из ленты {hidden} "
                 + labels.plural(hidden, "вакансию", "вакансии", "вакансий")
                 + " «under 18 år» — туда берут только тех, кому нет 18. "
                 f"Осталось {after}. Спроси ещё раз, что есть рядом.")
    else:
        reply = (f"Запомнил: тебе {years}. Вакансий «только до 18 лет» "
                 "в ленте сейчас нет — прятать было нечего.")
    return {"ok": True, "kind": "text", "reply": reply}


TOOLS: dict[str, Tool] = {
    tool.name: tool for tool in (
        Tool("search_jobs", "Поиск по ленте", {"query": ("str", MAX_QUERY)}, _tool_search),
        Tool("nearby_jobs", "Что есть рядом", {"query": ("str", MAX_QUERY)}, _tool_nearby),
        Tool("recommend_jobs", "Подходящее мне", {}, _tool_recommend),
        Tool("explain_verdict", "Почему нужен датский", {"job_id": ("str", 220)}, _tool_explain_verdict),
        Tool("job_facts", "Детали вакансии", {"job_id": ("str", 220)}, _tool_job_facts),
        Tool("profile_gaps", "Чего не хватает для подачи", {}, _tool_profile_gaps),
        Tool("application_status", "Мои заявки", {}, _tool_application_status),
        Tool("prepare_application", "Подготовить заявку", {"job_id": ("str", 220)},
             _tool_prepare_application),
        Tool("set_age", "Запомнить возраст", {"age": ("int", 10, 99)}, _tool_set_age),
        Tool("update_profile", "Изменить профиль",
             {"field": ("str", 40), "value": ("str", 600)}, _tool_update_profile),
        Tool("undo_profile", "Вернуть как было", {}, _tool_undo_profile),
        Tool("open_page", "Открыть раздел", {"page": ("str", 60)}, _tool_open_page),
        Tool("help", "Что я умею", {}, _tool_help),
    )
}


def catalog() -> list[dict]:
    """Безопасное описание умений — им же будет пользоваться ИИ на этапе 6."""
    return [{"name": t.name, "human": t.human, "args": {k: v[0] for k, v in t.args.items()}}
            for t in TOOLS.values()]


def run(name: str, args: dict | None = None) -> dict:
    """Выполнить инструмент по имени. Неизвестное имя — вежливый отказ."""
    tool = TOOLS.get(str(name or "").strip())
    if tool is None:
        return {"ok": False, "kind": "text",
                "reply": "Я такого не умею. Могу найти вакансии, показать, что есть рядом, "
                         "объяснить вердикт о языке или подсказать, чего не хватает для подачи."}
    try:
        return {**tool.run(_clean_args(tool, args or {})), "tool": tool.name,
                "tool_human": tool.human}
    except Exception:  # noqa: BLE001 — помощник не имеет права ронять страницу
        return {"ok": False, "kind": "text", "tool": tool.name, "tool_human": tool.human,
                "reply": "Не получилось это сделать. Попробуй ещё раз."}


# ── разбор просьбы без ИИ ──────────────────────────────────────────────────

_NEARBY_WORDS = ("рядом", "поблизости", "недалеко", "около", "близко", "рядышком")
_RECOMMEND_WORDS = ("подходит", "подходящ", "рекоменд", "посоветуй", "что мне")
_WHY_WORDS = ("почему", "зачем", "объясни")
_PROFILE_WORDS = ("профил", "чего не хватает", "что заполнить", "готов ли я")
_STATUS_WORDS = ("мои заявки", "мои отклики", "что с подач", "статус подач")
_APPLY_WORDS = ("подайся", "подать", "откликнись", "отправь заявку")
_CHANGE_WORDS = ("поставь", "измени", "поменяй", "запиши", "исправь", "укажи",
                 "смени", "обнови", "сохрани", "выстави")
_CHANGE_FILLER = frozenset({
    "мой", "моя", "моё", "мне", "в", "на", "у", "меня", "это", "теперь",
    "пожалуйста", "будет", "равно", "как", "профиле", "профиль",
})
_NEGATION = re.compile(r"\bне\s+(?:готов|могу|хочу|буду)|\bнет\b", re.I)
_UNDO_WORDS = ("верни как было", "верни обратно", "отмени изменение",
               "отмени правку", "верни назад")
# «Открой вакансии» — просьба перейти, а не искать. Проверяется ПОСЛЕ команд
# правки профиля: «открой профиль и поставь город» — это всё-таки правка.
_OPEN_WORDS = ("открой", "открыть", "перейди", "покажи страницу", "зайди в",
               "переключи на", "отведи")
_GREETINGS = frozenset({
    "привет", "здравствуй", "здравствуйте", "хай", "ку", "hej", "hello", "hi",
    "добрый день", "доброе утро", "добрый вечер", "прив",
})
_HELP_WORDS = ("что ты умеешь", "что умеешь", "чем поможешь", "что можешь",
               "помощь", "как пользоваться", "что тут делать")
# Слова-связки вокруг возраста: с ними фраза всё ещё «только про возраст».
_AGE_FILLER = frozenset({
    "мне", "я", "уже", "лет", "года", "год", "годика", "исполнилось",
    "мой", "моя", "возраст", "а", "и", "кстати", "вообще", "то", "есть",
    ".", ",", "!",
})


def _guess_profile_change(low: str) -> dict | None:
    """Просьба изменить профиль, разобранная без ИИ.

    Требуем явное слово-команду («поставь», «измени»): иначе «в моём городе
    ничего нет» превратилось бы в правку профиля. Значение — то, что идёт
    после названия поля.
    """
    import assistant_profile

    if not any(word in low for word in _CHANGE_WORDS):
        return None
    field = assistant_profile.guess_field(low)
    if not field:
        return None
    alias = max(
        (name for name, key in assistant_profile.ALIASES.items()
         if key == field and name in low),
        key=len, default="",
    )
    tail = low.split(alias, 1)[1] if alias else ""
    value = " ".join(
        word for word in tail.replace("=", " ").split()
        if word not in _CHANGE_FILLER
    ).strip(" -–—:,.")
    if not value and assistant_profile.SOFT_FIELDS.get(field, ("", ""))[1] == "yesno":
        # «поставь готовность к ночным сменам» без ответа — считаем это «да»,
        # потому что человек просит именно включить, а не спросить.
        value = "нет" if _NEGATION.search(low) else "да"
    return {"field": field, "value": value} if value else None


def _only_age_statement(low: str) -> int | None:
    """Возраст, если человек сказал ТОЛЬКО его: «мне 20 лет», «20 лет».

    Если во фразе есть и поиск («нетто херлев, мне 20»), возраст остаётся
    фильтром одного запроса и вакансии ищет обычный поиск. Насовсем запоминаем
    только тогда, когда человек больше ничего не просил, — иначе одна оговорка
    молча меняла бы всю ленту.
    """
    import query_parse

    years = query_parse.stated_age(low)
    if years is None:
        return None
    rest = query_parse._RE_MY_AGE.sub(" ", low)
    rest = " ".join(word for word in rest.replace("-", " ").split()
                    if word not in _AGE_FILLER)
    return years if not rest else None


def guess_tool(text: str, *, job_id: str = "") -> tuple[str, dict]:
    """Понять просьбу словарями — тем же способом, что и поиск.

    Без ИИ помощник обязан оставаться полезным: у большинства людей ключа нет,
    а у остальных однажды кончится дневная квота.
    """
    low = " ".join(str(text or "").lower().split())
    if not low:
        return "", {}
    if any(word in low for word in _APPLY_WORDS) and job_id:
        return "prepare_application", {"job_id": job_id}
    age = _only_age_statement(low)
    if age is not None:
        return "set_age", {"age": age}
    if any(word in low for word in _UNDO_WORDS):
        return "undo_profile", {}
    change = _guess_profile_change(low)
    if change is not None:
        return "update_profile", change
    if low.strip(" .!?…") in _GREETINGS or any(word in low for word in _HELP_WORDS):
        return "help", {}
    if any(word in low for word in _OPEN_WORDS):
        return "open_page", {"page": text}
    if any(word in low for word in _STATUS_WORDS):
        return "application_status", {}
    if any(word in low for word in _PROFILE_WORDS):
        return "profile_gaps", {}
    if any(word in low for word in _WHY_WORDS) and job_id:
        return "explain_verdict", {"job_id": job_id}
    if any(word in low for word in _RECOMMEND_WORDS):
        return "recommend_jobs", {}
    if any(word in low for word in _NEARBY_WORDS):
        return "nearby_jobs", {"query": text}
    return "search_jobs", {"query": text}


def _router_schema() -> dict:
    """Единственный формат, который модель вправе вернуть."""
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": list(TOOLS)},
            "args": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": MAX_QUERY},
                    "job_id": {"type": "string", "maxLength": 220},
                    "age": {"type": "integer", "minimum": 10, "maximum": 99},
                    "field": {"type": "string", "maxLength": 40},
                    "value": {"type": "string", "maxLength": 600},
                    "page": {"type": "string", "maxLength": 60},
                },
                "additionalProperties": False,
            },
        },
        "required": ["tool", "args"],
        "additionalProperties": False,
    }


def clean_history(history) -> list[dict]:
    """Последние реплики диалога в безопасном виде.

    Без них помощник читает каждое сообщение как первое: «мне 20 лет» после
    «что есть рядом» превращалось в поиск по тексту вакансий. Держим короткий
    хвост — этого хватает на уточнения и не раздувает запрос к модели.
    """
    turns = []
    for item in list(history or [])[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        role = "me" if str(item.get("role") or "") == "me" else "bot"
        text = " ".join(str(item.get("text") or "").split())[:MAX_HISTORY_CHARS]
        if text:
            turns.append({"role": role, "text": text})
    return turns


def _router_prompt(text: str, job_id: str, history=None, avoid: str = "") -> str:
    """Промпт содержит только запрос и разрешённый диспетчеру контекст."""
    request = " ".join(str(text or "").split())[:MAX_QUERY]
    current_job_id = " ".join(str(job_id or "").split())[:220]
    context = {key: str(value)[:160] for key, value in person_context().items()}
    payload = {
        "request": request,
        "catalog": catalog(),
        "person_context": context,
    }
    turns = clean_history(history)
    if turns:
        payload["history"] = turns
    if avoid:
        payload["already_tried"] = avoid
        payload["hint"] = (
            "Предыдущий инструмент ничего не нашёл. Выбери другой или те же "
            "поиск с более простым запросом — например, только город или "
            "только название магазина."
        )
    if current_job_id:
        payload["job_id"] = current_job_id
    return (
        "Ты диспетчер инструментов. Верни только JSON по схеме: выбери один "
        "инструмент из catalog и только его аргументы. Не отвечай на вопрос, "
        "не сообщай факты и не придумывай job_id.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _valid_ai_route(data, *, current_job_id: str) -> tuple[str, dict] | None:
    """Проверить ответ локально: провайдеры не одинаково строго применяют schema."""
    if not isinstance(data, dict) or set(data) != {"tool", "args"}:
        return None
    name = data.get("tool")
    args = data.get("args")
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None or not isinstance(args, dict):
        return None
    if not set(args).issubset(tool.args):
        return None
    for key, value in args.items():
        spec = tool.args[key]
        if spec[0] == "str" and not isinstance(value, str):
            return None
        if spec[0] == "int" and (not isinstance(value, int) or isinstance(value, bool)):
            return None

    clean = _clean_args(tool, args)
    if set(clean) != set(args):
        return None
    if "job_id" in tool.args:
        expected = " ".join(str(current_job_id or "").split())[:220]
        # Идентификатор вакансии приходит только из открытой карточки, не от модели.
        if not expected or clean.get("job_id") != expected:
            return None
    return name, clean


def _ai_route(text: str, *, job_id: str, history=None,
              avoid: str = "") -> tuple[str, dict] | None:
    """Попросить ИИ выбрать инструмент; любая ошибка означает обычный fallback."""
    import ai_gateway

    try:
        if not ai_gateway.available():
            return None
        result = ai_gateway.generate_json(
            _router_prompt(text, job_id, history=history, avoid=avoid),
            schema=_router_schema(),
            temperature=0.0,
            max_tokens=160,
            timeout=AI_ROUTER_TIMEOUT,
            retries=0,
        )
    except Exception:  # noqa: BLE001 — отсутствие ИИ не должно ломать помощника
        return None
    if not getattr(result, "ok", False):
        return None
    return _valid_ai_route(getattr(result, "data", None), current_job_id=job_id)


def _wording_schema() -> dict:
    return {
        "type": "object",
        "properties": {"reply": {"type": "string", "maxLength": MAX_REPLY}},
        "required": ["reply"],
        "additionalProperties": False,
    }


def _wording_prompt(text: str, result: dict, history=None) -> str:
    """Промпт для формулировки: модель получает ТОЛЬКО данные инструмента."""
    facts = {
        "request": " ".join(str(text or "").split())[:MAX_QUERY],
        "tool": str(result.get("tool_human") or ""),
        "app_reply": str(result.get("reply") or "")[:600],
        "found": len(result.get("results") or []),
        "items": [
            {key: str(card.get(key) or "")[:120]
             for key in ("title", "subtitle") if card.get(key)}
            for card in (result.get("results") or [])[:MAX_RESULTS]
        ],
    }
    turns = clean_history(history)
    if turns:
        facts["history"] = turns
    return (
        "Ты — помощник в приложении для поиска работы в Дании. Приложение уже "
        "выполнило запрос и прислало готовые данные. Напиши ответ человеку "
        "по-русски: 1–3 коротких предложения, дружелюбно и по делу. "
        "Обращайся на «ты» — так говорит всё приложение; «вы» звучит чужеродно.\n"
        "Строгие правила:\n"
        "1. Опирайся ТОЛЬКО на присланные данные. Не добавляй вакансии, "
        "адреса, часы, зарплаты, названия магазинов и числа, которых в них нет.\n"
        "2. Карточки вакансий человек уже видит под твоим текстом — не "
        "перечисляй их подряд, лучше скажи главное и подскажи следующий шаг.\n"
        "3. Ничего не обещай отправить или подать: заявку человек отправляет сам.\n"
        "4. Если данных мало или ничего не нашлось — так и скажи и предложи, "
        "как переспросить.\n"
        "Верни только JSON вида {\"reply\": \"…\"}.\n"
        "ДАННЫЕ: " + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    )


def _clean_wording(value) -> str:
    """Оставить обычный текст: разметка и ссылки от модели нам не нужны."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]*>", " ", value)
    text = re.sub(r"https?://\S+", " ", text)
    return " ".join(text.split())[:MAX_REPLY]


def _ai_wording(text: str, result: dict, history=None) -> str:
    """Человеческая формулировка поверх фактов. Любой сбой — пустая строка."""
    if not result.get("ok") or result.get("kind") == "confirm":
        return ""
    import ai_gateway

    try:
        if not ai_gateway.available():
            return ""
        answer = ai_gateway.generate_json(
            _wording_prompt(text, result, history=history),
            schema=_wording_schema(),
            temperature=0.3,
            max_tokens=220,
            timeout=AI_REPLY_TIMEOUT,
            retries=0,
        )
    except Exception:  # noqa: BLE001 — без ИИ помощник обязан остаться прежним
        return ""
    if not getattr(answer, "ok", False):
        return ""
    data = getattr(answer, "data", None)
    if not isinstance(data, dict):
        return ""
    return _clean_wording(data.get("reply"))


def _found_nothing(result: dict) -> bool:
    """Инструмент отработал, но показывать нечего — повод попробовать иначе."""
    return bool(result.get("ok")) and result.get("kind") in ("jobs", "cards") \
        and not (result.get("results") or [])


def ask(text: str, *, job_id: str = "", history=None) -> dict:
    """Ответить фактами приложения; ИИ выбирает инструмент и формулирует ответ.

    Порядок ровно такой, как принято у продуктовых ассистентов: модель видит
    хвост диалога и каталог умений, выбирает действие, приложение выполняет его
    ПО СВОЕЙ БАЗЕ, а модель пересказывает результат словами. Если первый выбор
    ничего не нашёл, ей дают ровно одну вторую попытку — это заметно умнее
    ответа «ничего не нашлось» и при этом не превращается в бесконечный цикл.
    """
    turns = clean_history(history)
    fallback_name, fallback_args = guess_tool(text, job_id=job_id)
    if not fallback_name:
        return {"ok": True, "kind": "text", "used_ai": False,
                "reply": "Напиши, что ищешь: «нетто херлев», «что есть рядом», "
                         "«что мне подходит» или «чего не хватает для подачи»."}
    routed = _ai_route(text, job_id=job_id, history=turns)
    if routed is None:
        result = {**run(fallback_name, fallback_args), "used_ai": False}
    else:
        name, args = routed
        result = {**run(name, args), "used_ai": True}
        if _found_nothing(result):
            retry = _ai_route(text, job_id=job_id, history=turns, avoid=name)
            if retry is not None and retry != (name, args):
                second = {**run(retry[0], retry[1]), "used_ai": True}
                if not _found_nothing(second):
                    result = {**second, "retried": True}
    worded = _ai_wording(text, result, history=turns)
    if worded:
        # Меняем ТОЛЬКО слова. Карточки, ссылки и кнопки остаются те, что
        # посчитало приложение: модель не вправе создать вакансию текстом.
        result = {**result, "reply": worded, "ai_wording": True}
    return result


def person_context() -> dict:
    """Что помощник знает о человеке. Узкий белый список — и ничего сверх него."""
    import profile_store

    profile = profile_store.load_profile()
    known = {key: str(profile.get(key) or "").strip()
             for key in CONTEXT_WHITELIST if str(profile.get(key) or "").strip()}
    # Дом нужен для расстояний, но адрес помощнику не нужен: расстояние считает
    # приложение, а модели достаточно знать, что дом вообще задан.
    import settings_store
    known["home_set"] = "да" if settings_store.get_home() else "нет"
    return known
