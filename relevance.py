"""Подойдёт ли вакансия человеку без датского языка и без датского диплома.

Шаг 2 пересмотра продукта 08.08.2026. Аудитория WexFlow — украинцы и
русскоязычные в Дании, поэтому «открытая вакансия в Дании» и «вакансия, на
которую тебя реально возьмут» — разные вещи. Здесь считается вердикт.

Как устроено (два слоя, дешёвый первым):

1. **Правила по тексту вакансии — бесплатно и всегда.** Ловим только ПРЯМЫЕ
   утверждения: «dansk i tale og skrift» → нужен датский; «English is our
   working language» → подойдёт; «dansk autorisation» → нужен местный диплом.
   Отрицания («dansk er ikke et krav») проверяются первыми. Если текст молчит —
   правила молчат тоже, а не угадывают.

2. **ИИ по РОЛЯМ, а не по вакансиям.** В базе 2549 открытых датских вакансий,
   но всего ~900 разных ролей: «butiksassistent under 18 år» повторяется 191
   раз. Судим роль один раз, ответ кладём в таблицу RoleVerdict и раздаём всем
   её вакансиям. Так полный проход стоит ~37 запросов вместо 2549, а дальше —
   единицы в день на новые роли. Частые роли судим первыми: после десятка
   запросов размечено больше половины ленты.

Честность: вердикт всегда несёт причину, которую человек может проверить, и
видно, чем он получен — цитатой из текста или мнением ИИ. Без ключа ИИ модуль
работает, просто чаще отвечает «не ясно». «Не ясно» из ленты НЕ убирается:
скрываем только уверенное «нужен датский / нужен местный диплом».
"""
from __future__ import annotations

import hashlib
import html
import re

from db import Job, RoleVerdict, get_session, select, utcnow

# Вердикты. Порядок важен: чем «хуже», тем сильнее — правила бьют ИИ.
OK = "ok"                # возьмут без датского и без местного диплома
DANISH = "danish"        # нужен разговорный/письменный датский
DIPLOMA = "diploma"      # нужен датский диплом, авторизация или лицензия
UNCLEAR = "unclear"      # по тексту не понять
VERDICTS = (OK, DANISH, DIPLOMA, UNCLEAR)

# Что скрывается из ленты, когда фильтр включён (по умолчанию включён).
BARRIER = (DANISH, DIPLOMA)

LABELS = {
    OK: "без датского",
    DANISH: "нужен датский",
    DIPLOMA: "нужен местный диплом",
    UNCLEAR: "не ясно",
}

ROLE_BATCH = 25          # сколько ролей в одном запросе к ИИ
MAX_ROLE_CHARS = 90
SNIPPET_CHARS = 220

# Версия правил по тексту. Меняешь регулярки ниже — подними число, и вердикты
# пересчитаются у всех сами при ближайшем обновлении базы.
RULES_VERSION = 1


# ── Слой 1: правила по тексту ──────────────────────────────────────────────
# Сначала отрицания: «датский не обязателен» встречается и в вакансиях, где
# рядом стоит слово «dansk», поэтому проверяем их ПЕРВЫМИ.
_NO_DANISH_NEEDED = (
    r"dansk\s+er\s+ikke\s+(et\s+)?krav",
    r"(beh[øo]ver|kr[æa]ver)\s+ikke\s+(at\s+)?(tale|kunne)\s+dansk",
    r"ikke\s+n[øo]dvendigt\s+at\s+(tale|kunne)\s+dansk",
    r"no\s+danish\s+(is\s+)?(required|needed|necessary)",
    r"danish\s+is\s+not\s+(a\s+)?(requirement|required|needed)",
    r"you\s+(do\s+not|don'?t)\s+need\s+to\s+speak\s+danish",
    r"english\s+is\s+(our|the)\s+(working|corporate|company)\s+language",
    r"engelsk\s+er\s+(vores|virksomhedens)\s+(arbejdssprog|koncernsprog)",
    r"vi\s+taler\s+engelsk\s+p[åa]\s+arbejdet",
)

_DANISH_REQUIRED = (
    r"dansk\s+i\s+(tale\s+og\s+skrift|skrift\s+og\s+tale)",
    r"tale[r]?\s+og\s+skrive[r]?\s+dansk",
    r"flydende\s+dansk",
    r"dansk\s+p[åa]\s+(h[øo]jt\s+niveau|modersm[åa]lsniveau)",
    r"gode\s+dansk\s*kundskaber",
    r"dansk\s*kundskaber\s+er\s+(et\s+)?(krav|n[øo]dvendig)",
    r"beherske[r]?\s+dansk",
    r"du\s+(taler|skriver)\s+(og\s+skriver\s+)?dansk",
    r"kr[æa]ver\s+(gode\s+)?dansk",
    r"fluent\s+(in\s+)?danish",
    r"danish\s+(language\s+)?(skills?|proficiency)\s+(is\s+)?(required|a\s+must)",
    r"you\s+(must\s+)?speak\s+danish",
)

_DIPLOMA_REQUIRED = (
    r"dansk\s+autorisation",
    r"autorisation\s+som\s+(sygeplejerske|social|s[øo]su|l[æa]ge|farmaceut)",
    r"dansk\s+(uddannelse|eksamen|bevis)\s+(som|inden\s+for)",
    r"uddannet\s+(sygeplejerske|p[æa]dagog|s[øo]su|elektriker|smed|tandl[æa]ge)",
    r"(bachelor|kandidat|professionsbachelor)\s*grad",
    r"dansk\s+k[øo]rekort\s+(er\s+et\s+krav|kr[æa]ves)",
    r"danish\s+(authorisation|authorization|licen[cs]e)\s+(is\s+)?(required|needed)",
)

_NO_DANISH_RE = re.compile("|".join(_NO_DANISH_NEEDED), re.I)
_DANISH_RE = re.compile("|".join(_DANISH_REQUIRED), re.I)
_DIPLOMA_RE = re.compile("|".join(_DIPLOMA_REQUIRED), re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(job) -> str:
    """Название + описание одним текстом без разметки.

    Сущности раскрываем обязательно: у Salling описание приходит как
    «dansk i tale og skrift» вперемешку с &aring;/&oslash;, и без unescape
    правила не увидели бы половину датских слов.
    """
    raw = f"{getattr(job, 'title', '') or ''}\n{getattr(job, 'description', '') or ''}"
    text = html.unescape(_TAG_RE.sub(" ", raw))
    return re.sub(r"\s+", " ", text.replace("\xa0", " "))


def _quote(text: str, match: re.Match) -> str:
    """Кусок текста вокруг совпадения — чтобы человек мог проверить вердикт."""
    start = max(0, match.start() - 25)
    end = min(len(text), match.end() + 25)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def by_rules(job) -> tuple[str, str] | None:
    """Вердикт по прямым словам вакансии или None, если текст молчит."""
    text = _plain(job)
    if not text.strip():
        return None
    if (m := _NO_DANISH_RE.search(text)):
        return OK, f"в тексте: «{_quote(text, m)}»"
    if (m := _DANISH_RE.search(text)):
        return DANISH, f"в тексте: «{_quote(text, m)}»"
    if (m := _DIPLOMA_RE.search(text)):
        return DIPLOMA, f"в тексте: «{_quote(text, m)}»"
    return None


# ── Слой 2: роли ───────────────────────────────────────────────────────────
_NON_LETTERS = re.compile(r"[^a-zæøåÆØÅ ]+", re.I)


def role_phrase(job) -> str:
    """Название вакансии без города и цифр: «Butiksassistent under 18 år
    Kgs. Lyngby» → «butiksassistent under år». Именно это и повторяется."""
    title = str(getattr(job, "title", "") or "").lower()
    city = " ".join(str(getattr(job, "city", "") or "").lower().split())
    # Сначала город целиком: у «København S» и «Kgs. Lyngby» вторая часть —
    # район, и по отдельным словам она бы осталась в названии роли.
    if city:
        title = title.replace(city, " ")
    for word in re.split(r"[\s,./-]+", city):
        if len(word) > 2:
            title = title.replace(word, " ")
    title = _NON_LETTERS.sub(" ", title)
    return " ".join(title.split())[:MAX_ROLE_CHARS]


def role_key(job) -> str:
    """Подпись роли: источник + категория + название без города."""
    source = str(getattr(job, "source", "") or "")
    category = str(getattr(job, "categories", "") or "").split(",")[0]
    phrase = role_phrase(job)
    if not phrase:
        return ""
    raw = f"{source}|{category}|{phrase}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def text_hash(job) -> str:
    """Отпечаток текста: пока он тот же, вердикт пересчитывать незачем.

    В отпечаток входит версия правил: когда правила меняются с обновлением
    WexFlow, все вердикты обязаны пересчитаться сами, иначе человек остался бы
    со старыми оценками навсегда.
    """
    blob = (f"{RULES_VERSION}\x00{getattr(job, 'title', '') or ''}"
            f"\x00{getattr(job, 'description', '') or ''}")
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _snippet(job) -> str:
    text = _plain(job)
    # начало описания, без названия — оно и так уходит отдельным полем
    body = text[len(str(getattr(job, "title", "") or "")):].strip()
    return body[:SNIPPET_CHARS]


def _prompt(roles: list[dict]) -> str:
    lines = []
    for i, role in enumerate(roles, start=1):
        lines.append(
            f"{i}. Должность: {role['phrase']}"
            + (f" | категория: {role['category']}" if role["category"] else "")
            + (f" | работодатель: {role['brand']}" if role.get("brand") else "")
            + (f"\n   Из описания: {role['snippet']}" if role["snippet"] else "")
        )
    return (
        "Ты помогаешь иностранцу в Дании понять, на какую работу его реально "
        "возьмут. Человек НЕ говорит по-датски (английский на бытовом уровне, "
        "родной русский или украинский) и у него НЕТ датского образования, "
        "диплома и профессиональной авторизации.\n\n"
        "Для каждой должности из списка ответь одним вердиктом:\n"
        f"- \"{OK}\" — работу такого рода в Дании реально получить без датского "
        "языка и без местного диплома (склад, уборка, производство, мойка "
        "посуды, курьер, разнорабочий и подобное);\n"
        f"- \"{DANISH}\" — работа на практике требует датского, потому что это "
        "постоянное общение с датскими покупателями, коллегами или документами "
        "(касса, торговый зал, обслуживание, руководство, офис);\n"
        f"- \"{DIPLOMA}\" — нужен датский диплом, авторизация или лицензия "
        "(медицина, педагогика, охрана, электрик, вождение грузовика);\n"
        f"- \"{UNCLEAR}\" — по названию и описанию честно не понять.\n\n"
        "Правила: не выдумывай требований, которых нет; если сомневаешься между "
        f"\"{OK}\" и \"{DANISH}\" — ставь \"{UNCLEAR}\"; причина — одно короткое "
        "предложение ПО-РУССКИ, не длиннее 90 символов.\n\n"
        "Отвечай СТРОГО JSON-объектом вида "
        '{"roles":[{"n":1,"verdict":"ok","reason":"…"}]} без текста вокруг.\n\n'
        "Должности:\n" + "\n".join(lines)
    )


def _parse_batch(data: dict, roles: list[dict]) -> dict[int, tuple[str, str]]:
    """Ответ ИИ → {номер: (вердикт, причина)}. Мусор молча выбрасываем."""
    out: dict[int, tuple[str, str]] = {}
    items = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= len(roles):
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        reason = " ".join(str(item.get("reason") or "").split())[:150]
        out[number] = (verdict, reason)
    return out


def _pending_roles(session, limit: int) -> list[dict]:
    """Роли без вердикта — самые частые первыми (ими размечается вся лента)."""
    import feed

    rows = session.exec(
        select(Job).where(*feed.visible_clauses(fit=False))
    ).all()
    known = {r.key for r in session.exec(select(RoleVerdict)).all()}
    groups: dict[str, dict] = {}
    for job in rows:
        if by_rules(job) is not None:
            continue                       # текст уже всё сказал, ИИ не нужен
        key = role_key(job)
        if not key or key in known:
            continue
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "key": key,
                "source": str(job.source or ""),
                "category": str(job.categories or "").split(",")[0],
                "brand": str(job.brand or ""),
                "phrase": role_phrase(job),
                "snippet": _snippet(job),
                "count": 1,
            }
        else:
            group["count"] += 1
            if not group["snippet"]:
                group["snippet"] = _snippet(job)
    ordered = sorted(groups.values(), key=lambda g: -g["count"])
    return ordered[: max(0, int(limit))]


def judge_roles(max_requests: int = 2) -> dict:
    """Спросить ИИ про роли без вердикта. Возвращает сводку прогона.

    max_requests ограничивает расход: одна порция — ROLE_BATCH ролей.
    """
    import ai_filters

    report = {"asked": 0, "judged": 0, "requests": 0, "error": ""}
    if max_requests <= 0:
        return report
    if not (ai_filters.available() or ai_filters.gemini_available()):
        report["error"] = "ИИ не подключён"
        return report
    with get_session() as session:
        roles = _pending_roles(session, ROLE_BATCH * max_requests)
    if not roles:
        return report

    now = utcnow()
    for start in range(0, len(roles), ROLE_BATCH):
        batch = roles[start:start + ROLE_BATCH]
        answer = ai_filters.generate_json(_prompt(batch), temperature=0.0, timeout=60)
        report["requests"] += 1
        report["asked"] += len(batch)
        if not answer.get("ok"):
            report["error"] = str(answer.get("error") or "ИИ не ответил")[:200]
            break
        parsed = _parse_batch(answer.get("data") or {}, batch)
        engine = f"ai:{answer.get('model') or 'ai'}"[:60]
        if not parsed:
            report["error"] = "ИИ вернул ответ без вердиктов"
            break
        with get_session() as session:
            for number, (verdict, reason) in parsed.items():
                role = batch[number - 1]
                row = session.get(RoleVerdict, role["key"]) or RoleVerdict(key=role["key"])
                row.source = role["source"][:40]
                row.category = role["category"][:80]
                row.role = role["phrase"][:MAX_ROLE_CHARS]
                row.verdict = verdict
                row.reason = reason[:150]
                row.engine = engine
                row.updated_at = now
                session.add(row)
                report["judged"] += 1
            session.commit()
    return report


# ── Раздача вердиктов вакансиям ────────────────────────────────────────────
def apply_to_jobs(session=None) -> dict:
    """Проставить вердикты вакансиям ленты. Дёшево: текст, не изменившийся с
    прошлого раза, не пересчитываем."""
    import feed

    own = session is None
    session = session or get_session()
    report = {"updated": 0, "counts": {v: 0 for v in VERDICTS}}
    try:
        roles = {r.key: r for r in session.exec(select(RoleVerdict)).all()}
        jobs = session.exec(select(Job).where(*feed.visible_clauses(fit=False))).all()
        for job in jobs:
            digest = text_hash(job)
            # Текст с прошлого раза не менялся — значит и правила скажут то же
            # самое. Регулярки по описанию (самая дорогая часть прохода) гоняем
            # только по новым и изменившимся вакансиям.
            evaluated = job.fit in VERDICTS and job.fit_hash == digest
            if evaluated and job.fit_engine == "rules":
                report["counts"][job.fit] += 1
                continue
            decided = None if evaluated else by_rules(job)
            if decided is not None:
                verdict, reason, engine = (*decided, "rules")
            else:
                role = roles.get(role_key(job))
                if role is not None:
                    verdict = role.verdict
                    reason = role.reason or "оценка ИИ по названию должности"
                    engine = role.engine or "ai"
                else:
                    verdict, reason, engine = UNCLEAR, "", ""
            report["counts"][verdict] += 1
            if (job.fit, job.fit_reason, job.fit_engine, job.fit_hash) == (
                    verdict, reason, engine, digest):
                continue
            job.fit = verdict
            job.fit_reason = reason[:200]
            job.fit_engine = engine[:60]
            job.fit_hash = digest
            job.fit_at = utcnow()
            session.add(job)
            report["updated"] += 1
        if report["updated"]:
            session.commit()
    finally:
        if own:
            session.close()
    return report


def refresh(max_requests: int = 2) -> dict:
    """Полный проход: спросить ИИ про новые роли и разложить вердикты.

    Вызывается после обновления базы. max_requests=0 — только правила и уже
    известные роли, ни одного запроса к ИИ.
    """
    ai = judge_roles(max_requests) if max_requests > 0 else {"requests": 0, "judged": 0}
    applied = apply_to_jobs()
    return {"ai": ai, **applied}


def stats() -> dict:
    """Сколько вакансий ленты в каком вердикте (для страницы «Состояние»)."""
    import feed
    from sqlmodel import func

    with get_session() as session:
        rows = session.exec(
            select(Job.fit, func.count(Job.id))
            .where(*feed.visible_clauses(fit=False))
            .group_by(Job.fit)
        ).all()
        roles_judged = session.exec(
            select(func.count(RoleVerdict.key))
        ).one() or 0
    counts = {v: 0 for v in VERDICTS}
    for value, count in rows:
        counts[str(value or UNCLEAR) if str(value or UNCLEAR) in VERDICTS else UNCLEAR] += int(count or 0)
    counts["total"] = sum(counts[v] for v in VERDICTS)
    counts["roles_judged"] = int(roles_judged)
    return counts


def is_barrier(job) -> bool:
    """Уверенное «не подойдёт»: нужен датский или местный диплом.
    «Не ясно» и неоценённое сюда НЕ попадают — молчание не повод прятать."""
    return str(getattr(job, "fit", "") or "") in BARRIER


def describe(job) -> dict:
    """Вердикт вакансии для интерфейса: метка, причина, чем получен."""
    verdict = str(getattr(job, "fit", "") or UNCLEAR)
    if verdict not in VERDICTS:
        verdict = UNCLEAR
    engine = str(getattr(job, "fit_engine", "") or "")
    return {
        "verdict": verdict,
        "label": LABELS[verdict],
        "reason": str(getattr(job, "fit_reason", "") or ""),
        "by_ai": engine.startswith("ai"),
        "engine": engine,
        "barrier": verdict in BARRIER,
    }
