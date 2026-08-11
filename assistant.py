"""Помощник сбоку: понимает просьбу и дёргает готовые умения приложения.

Зачем именно так. Чат, который «сам всё знает», врёт: модель придумает
вакансию, адрес и часы, и человек поедет в несуществующий магазин. Поэтому
здесь помощник — **не рассказчик, а диспетчер**: он выбирает ИНСТРУМЕНТ из
короткого белого списка, инструмент выполняет приложение по своей базе, и
человеку показываются карточки из результата, а не текст модели.

Порядок появления тоже осознанный: сначала (этап 4) помощник работает
**вообще без ИИ** — просьбу разбирает `guess_tool` теми же словарями, что и
поиск. Это значит, что помощник есть у всех, включая людей без ключа и с
исчерпанной квотой. ИИ (этап 6) добавится сверху и будет уметь ровно одно:
выбрать имя инструмента и аргументы. Ни одного факта из модели.

Три границы, которые нельзя переносить:
  1. **Только белый список.** Неизвестное имя инструмента или кривой аргумент
     — вежливый отказ, а не попытка догадаться. Никакого SQL «из текста».
  2. **Подача — не инструмент помощника.** `prepare_application` возвращает
     карточку подтверждения со ссылкой; жмёт человек. Отправить заявку через
     помощника нельзя вообще — это закреплено тестом-инвариантом.
  3. **О человеке — только по делу.** Наружу (в будущий промпт ИИ) уходит
     узкий список полей: город, языки, опыт. Ни почты, ни телефона, ни адреса,
     ни даты рождения, ни содержимого CV — по образцу `connectors/ai_fill`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import labels

MAX_RESULTS = 8
MAX_QUERY = 120

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
            clean[name] = max(spec[1], min(spec[2], number))
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
        stmt = select(Job).where(*feed.visible_clauses(), *(clauses or []))
        if extra is not None:
            stmt = stmt.where(extra)
        rows = list(session.exec(stmt.order_by(Job.published.desc()).limit(limit * 3)).all())
    import relevance
    rows = [j for j in rows if not relevance.is_barrier(j)]
    return rows[:limit]


# ── сами инструменты ───────────────────────────────────────────────────────

def _tool_search(args: dict) -> dict:
    import query_parse
    from db import Job, get_session, select

    text = args.get("query", "")
    with get_session() as session:
        known = [row for row in session.exec(select(Job.city).distinct()).all() if row]
    parsed = query_parse.parse(text, known_cities=known)
    jobs = _visible_jobs(query_parse.clauses(parsed))
    jobs, _dropped = query_parse.python_filter(parsed, jobs)
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
        pool = list(session.exec(select(Job).where(*feed.visible_clauses(fit=False))).all())
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
    import geo
    import relevance

    if not home:
        return {"ok": True, "kind": "text",
                "reply": "Чтобы показать ближнее, нужен домашний адрес — он задаётся в профиле.",
                "href": "/profile#home", "button": "Указать дом"}
    ranked = []
    for job in pool:
        if job.lat is None or job.lon is None or relevance.is_barrier(job):
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
            select(Job).where(*feed.visible_clauses(exclude_applied=True)).limit(400)
        ).all())
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

    total = applications.submitted_total_count()
    today = applications.submitted_today_count()
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
    except Exception as exc:  # noqa: BLE001 — помощник не имеет права ронять страницу
        return {"ok": False, "kind": "text", "tool": tool.name, "tool_human": tool.human,
                "reply": f"Не получилось это сделать: {str(exc)[:120]}"}


# ── разбор просьбы без ИИ ──────────────────────────────────────────────────

_NEARBY_WORDS = ("рядом", "поблизости", "недалеко", "около", "близко", "рядышком")
_RECOMMEND_WORDS = ("подходит", "подходящ", "рекоменд", "посоветуй", "что мне")
_WHY_WORDS = ("почему", "зачем", "объясни")
_PROFILE_WORDS = ("профил", "чего не хватает", "что заполнить", "готов ли я")
_STATUS_WORDS = ("мои заявки", "мои отклики", "что с подач", "статус подач")
_APPLY_WORDS = ("подайся", "подать", "откликнись", "отправь заявку")


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


def ask(text: str, *, job_id: str = "") -> dict:
    """Ответить на просьбу человека. Пока без ИИ — только словари и инструменты."""
    name, args = guess_tool(text, job_id=job_id)
    if not name:
        return {"ok": True, "kind": "text", "used_ai": False,
                "reply": "Напиши, что ищешь: «нетто херлев», «что есть рядом», "
                         "«что мне подходит» или «чего не хватает для подачи»."}
    return {**run(name, args), "used_ai": False}


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
